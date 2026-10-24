"""
税则通 - FastAPI 后端
提供商品编码与税率查询 API 接口
"""
import json
import os
import re
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# 版本号单一来源：FastAPI 元数据与 GET / 同读此常量，避免多处写死后互相矛盾
SERVICE_VERSION = "1.1.0"

# 配置
app = FastAPI(title="税则通API", version=SERVICE_VERSION)

# CORS：收窄到微信侧来源。
# 原配置 allow_origins=["*"] + allow_credentials=True 是被浏览器规范禁止的组合
# （带凭据的请求不允许通配来源），且等于把接口暴露给任意网页。
# 如需放开，请用环境变量显式覆盖，不要把通配写回代码：
#   ALLOW_ORIGINS="*"                          通配（此时自动关闭 credentials）
#   ALLOW_ORIGINS="https://a.com,https://b.com"
_DEFAULT_ORIGINS = ["https://servicewechat.com", "https://mp.weixin.qq.com"]
_origins_env = os.getenv("ALLOW_ORIGINS", "").strip()
ALLOW_ORIGINS = [o.strip() for o in _origins_env.split(",") if o.strip()] if _origins_env else list(_DEFAULT_ORIGINS)
_ALLOW_WILDCARD = ALLOW_ORIGINS == ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=not _ALLOW_WILDCARD,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

# 加载 HS Code 数据（完整8972条2026税则）
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
HS_DATA_FILE = os.path.join(DATA_DIR, "hs_codes_full.json")
if not os.path.exists(HS_DATA_FILE):
    HS_DATA_FILE = os.path.join(DATA_DIR, "hs_codes.json")
with open(HS_DATA_FILE, "r", encoding="utf-8") as f:
    _RAW_DATA = json.load(f)

# 数据文件支持两种形态，避免形态变化导致启动失败：
#   旧：裸数组 [...]（无任何版本信息）
#   新：{"meta": {版本/生效日期/条数/来源}, "items": [...]}
if isinstance(_RAW_DATA, dict):
    HS_CODES = _RAW_DATA.get("items", [])
    HS_DATA_META = _RAW_DATA.get("meta", {})
else:
    HS_CODES = _RAW_DATA
    HS_DATA_META = {}

if not isinstance(HS_CODES, list) or not HS_CODES:
    raise RuntimeError("tax data is empty or malformed: " + HS_DATA_FILE)


# ========== 最小限流（内存计数，无第三方依赖） ==========
# 目的：给可批量拉取的接口（分类查询、HS 详情）一个成本上限。
# 局限（已如实写在修复报告中）：计数在单进程内存里，云托管多实例/冷启动会归零，
# 因此它是"防误用"而不是"防攻击"。真正的防护应放在云托管网关或加容器鉴权。
RATE_LIMIT_WINDOW_SEC = 60
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "120"))  # 每 IP 每分钟
_RATE_BUCKETS = {}           # ip -> [window_start, count]
_RATE_BUCKETS_MAX = 10000    # 桶上限，超过则整体清理，防止内存无限增长


def _client_ip(request) -> str:
    """取真实客户端 IP：云托管/反代会在 X-Forwarded-For 里透传原始 IP。"""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit(request, limit: int = RATE_LIMIT_MAX) -> None:
    now = time.time()
    ip = _client_ip(request)
    if len(_RATE_BUCKETS) > _RATE_BUCKETS_MAX:
        _RATE_BUCKETS.clear()
    start, count = _RATE_BUCKETS.get(ip, (now, 0))
    if now - start >= RATE_LIMIT_WINDOW_SEC:
        start, count = now, 0
    count += 1
    _RATE_BUCKETS[ip] = (start, count)
    if count > limit:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

# 品目描述补全：从官方税则PDF提取的1228个品目描述（覆盖率100%）
HS4_FILE = os.path.join(DATA_DIR, "hs4_descriptions.json")
try:
    with open(HS4_FILE, "r", encoding="utf-8") as f:
        HS4_DESCRIPTIONS = json.load(f)
except Exception:
    HS4_DESCRIPTIONS = {}

def _get_hs4_desc(hs_code):
    """获取HS编码前4位的品目描述"""
    hs4 = hs_code.split(".")[0][:4] if "." in hs_code else hs_code[:4]
    return HS4_DESCRIPTIONS.get(hs4, "")


def _clean(text: str) -> str:
    """剥离字段**尾部**的税率/占位数字残留（如 '其他 10' -> '其他'）。

    只允许用于 description 这类历史上被写脏的字段。**不要用于 name**：
    name 尾部本来就可能带合法数字（实测 49 条，如 '维生素AD3'、'氖-3'、
    '种用 65①'），一律剥离会把商品名改错。
    """
    if not text:
        return ""
    return re.sub(r"[\s\d.]+$", "", text).strip()


def _strip_rate_suffix(text: str, tariff) -> str:
    """description 清污：**仅当**尾部数字整体等于该条 import_tariff 时才剥离。

    既去掉 '其他 10' 这种税率残留，又不会误伤
    '重量在10千克及以上，但在50'（尾部 50 与税率 0 不符，保留原样）。
    """
    if not text:
        return ""
    m = re.search(r"[\s\u3000]+(\d+(?:\.\d+)?)[\u2460-\u2473\u2206]*$", text)
    if m and tariff is not None:
        try:
            if abs(float(m.group(1)) - float(tariff)) < 1e-9:
                return text[: m.start()].strip()
        except (TypeError, ValueError):
            pass
    return text


# 口语词 → 税则专业词/相关词 同义词映射
SYNONYMS = {
    "口红": ["唇用化妆品", "唇", "化妆品", "3304.1000"],
    "唇膏": ["唇用化妆品", "唇", "化妆品", "3304.1000"],
    "唇彩": ["唇用化妆品", "唇", "化妆品"],
    "眼影": ["眼用化妆品", "化妆品"],
    "睫毛膏": ["眼用化妆品", "化妆品"],
    "粉底": ["化妆品", "粉"],
    "防晒霜": ["化妆品", "护肤"],
    "面膜": ["化妆品", "护肤"],
    "t恤": ["针织衬衫", "棉制", "T恤衫", "汗衫", "背心", "6109"],
    "t血": ["针织衬衫", "棉制", "T恤衫", "6109"],
    "短袖": ["针织衬衫", "棉制", "T恤衫", "汗衫", "6109"],
    "衬衫": ["针织衬衫", "棉制", "T恤衫", "6105", "6205"],
    "汗衫": ["棉制", "T恤衫", "背心", "6109"],
    "背心": ["棉制", "T恤衫", "背心", "6109"],
    "裤子": ["裤子", "长裤", "牛仔裤"],
    "牛仔裤": ["裤子", "牛仔裤"],
    "裙子": ["连衣裙", "裙子"],
    "连衣裙": ["连衣裙"],
    "外套": ["夹克", "上衣", "外套"],
    "羽绒服": ["羽绒服", "夹克"],
    "鞋子": ["鞋靴", "鞋"],
    "运动鞋": ["鞋靴", "运动鞋"],
    "高跟鞋": ["鞋靴"],
    "包包": ["包", "手提包", "背包"],
    "手提包": ["包", "手提包"],
    "背包": ["包", "背包"],
    "钱包": ["钱包", "包"],
    "手机": ["电话机", "移动电话", "智能手机", "8517.1300"],
    "智能手机": ["电话机", "移动电话", "智能手机", "8517.1300"],
    "手机壳": ["电话机", "保护壳"],
    "充电器": ["充电器", "电源"],
    "数据线": ["电缆", "电线"],
    "蓝牙耳机": ["耳机", "耳塞", "8518.3000"],
    "耳机": ["耳机", "耳塞", "8518.3000"],
    "电脑": ["自动数据处理设备", "便携式", "微型机", "8471"],
    "笔记本电脑": ["便携式自动数据处理设备", "其他", "自动数据处理设备", "8471.3090"],
    "手提电脑": ["便携式自动数据处理设备", "其他", "8471.3090"],
    "平板": ["平板电脑", "便携式", "自动数据处理设备", "8471.3010"],
    "平板电脑": ["平板电脑", "便携式", "自动数据处理设备", "8471.3010"],
    "键盘": ["键盘", "输入设备"],
    "鼠标": ["鼠标", "输入设备"],
    "手表": ["手表", "9102"],
    "智能手表": ["手表", "9102"],
    "玩具": ["玩具"],
    "乐高": ["玩具", "积木"],
    "书": ["书", "印刷品"],
    "书籍": ["书", "印刷品"],
    "奶粉": ["奶粉", "乳粉", "0402"],
    "咖啡": ["咖啡", "0901.2100", "0901", "焙炒咖啡"],
    "咖啡豆": ["咖啡", "0901.2100", "0901"],
    "生咖啡豆": ["未焙炒咖啡", "0901.1100", "0901"],
    "速溶咖啡": ["咖啡精汁", "2101.1100", "2101"],
    "茶叶": ["茶", "0902"],
    "红酒": ["葡萄酒", "2204"],
    "葡萄酒": ["葡萄酒", "2204"],
    "啤酒": ["麦芽酿造的啤酒", "2203"],
    "白酒": ["蒸馏酒", "2208"],
    "椅子": ["坐具", "椅子", "9401"],
    "办公椅": ["坐具", "椅子", "9401"],
    "桌子": ["桌子", "办公", "9403"],
    "书桌": ["桌子", "办公", "9403"],
    "家具": ["家具", "9403"],
    "灯": ["灯具", "照明", "9405"],
    "台灯": ["灯具", "照明", "9405"],
    "电池": ["电池", "8507"],
    "锂电池": ["锂离子电池", "蓄电池", "8507.6000"],
    "充电宝": ["锂离子电池", "8507.6000", "蓄电池", "移动电源"],
    "移动电源": ["锂离子电池", "8507.6000", "蓄电池"],
    "雨伞": ["雨伞", "伞", "6601"],
    "眼镜": ["眼镜", "9004"],
    "太阳镜": ["眼镜", "太阳镜", "9004"],
    "戒指": ["首饰", "戒指", "7117"],
    "项链": ["首饰", "项链", "7117"],
    "化妆品": ["化妆品", "3304"],
    "护肤品": ["化妆品", "护肤", "3304"],
    "水杯": ["杯", "餐具", "3924", "6912", "7323"],
    "杯子": ["杯", "餐具", "3924", "6912", "7323"],
    "保温杯": ["杯", "7323", "3924"],
    "锅": ["厨房器具", "炊具", "7323"],
    "不锈钢锅": ["厨房器具", "炊具", "7323.9300"],
    "不粘锅": ["厨房器具", "炊具", "7323"],
    "充电器": ["充电器", "电源", "8504"],
    "数据线": ["电缆", "电线", "8544"],
    "零食": ["食品", "零食"],
    "巧克力": ["巧克力", "1806"],
    "饼干": ["饼干", "食品"],
    "坚果": ["坚果", "食品"],
    "乌龙茶": ["半发酵茶", "茶叶", "0902"],
    "罗非鱼": ["熏鱼", "鱼", "0305"],
    "乳清粉": ["乳清", "0404"],
    "鹅肝": ["肥肝", "鹅肝"],
    "鲸鱼油": ["鲸油", "海生哺乳动物", "1504"],
    "馄饨": ["包馅面食", "饺子", "1902"],
    "蜂胶": ["蜂胶", "保健食品", "2106"],
    "菊花": ["菊花", "药料用植物", "1211"],
    "牛油果": ["鳄梨", "0804"],
    "鸡肝": ["动物肝", "1602"],
    "冰淇淋": ["冰制食品", "雪糕", "2105"],
    "棒棒糖": ["糖果", "硬糖", "1704"],
    "蛋糕": ["糕点", "烘焙糕饼", "1905"],
    "松子": ["松仁", "坚果", "0802"],
    "果汁": ["水果汁", "2009"],
    "扁豆": ["豇豆", "菜豆", "0708"],
    "汾酒": ["白酒", "蒸馏酒", "2208"],
    "粽子": ["糯米制品", "谷物制品", "1904"],
    "活鸡": ["家禽", "鸡", "0105"],
    "种鸡": ["改良种用", "0105.9410"],
    "绿茶": ["茶叶", "0902"],
    "螃蟹": ["蟹", "0306"],
    "大闸蟹": ["0306.3391"],
    "刺身": ["鱼片", "0304"],
    # 俗称 → 税则用词。税则正文里只有"鲑科鱼/犀鳕科/中华绒螯蟹/杂碎"这类
    # 专业名，查不到"三文鱼/黑线鳕/醉蟹/蹄筋"这些市面叫法，所以这里补的是
    # **商品俗称**，不是某条测试串的特例；同义扩展是既有机制，品目取向仍由
    # 下面的加工状态词规则决定（如盐腌→0305、鲜→0709）。
    "三文鱼": ["鲑科鱼", "大麻哈鱼", "0304"],
    "黑线鳕": ["犀鳕科", "鳕", "0305"],
    "醉蟹": ["中华绒螯蟹", "0306"],
    # 蹄筋=猪的食用杂碎（鲜冷归0206、干盐腌归0210）。用"猪杂碎"而不是泛词
    # "杂碎"，否则家禽杂碎(0207)/肉骨粉(23章)会一起被拉进来。
    "蹄筋": ["猪杂碎", "0210", "0206"],
    # 蘑菇只指向"菌属"级别的税则用词，不写 0709/2003/0711 这类品目号：
    # 否则品目内所有税号同分会并列，鲜蘑菇会排到芦笋(0709.20)上。
    "蘑菇": ["蘑菇及块菌", "伞菌属蘑菇", "白蘑菇"],
    "小麦粉": ["面粉", "1101"],
    "花生油": ["1508"],
    "番茄酱": ["番茄沙司", "2103"],
    "方便面": ["即食面", "1902"],
    "薯片": ["马铃薯片", "2005"],
    "牛肉干": ["牛肉制品", "1602"],
    "西装": ["西服", "套装", "6203"],
    "羊毛衫": ["针织套头衫", "6110"],
    "皮鞋": ["皮革面鞋", "6403"],
    "皮衣": ["皮革衣服", "4203"],
    "塑料杯": ["塑料餐具", "3924"],
    "玻璃杯": ["玻璃器皿", "7013"],
    "陶瓷碗": ["陶瓷餐具", "6912"],
    "纸箱": ["纸板箱", "4819"],
    "羽毛球拍": ["球拍", "9506"],
    "自行车": ["脚踏车", "8712"],
    "空调": ["空气调节器", "8415"],
    "冰箱": ["冷藏冷冻箱", "8418"],
    "手机壳": ["手机保护壳", "3926"],
    "枕头": ["寝具", "9404"],
    "黄酒": ["发酵饮料", "2206"],
    "食盐": ["食用盐", "2501"],
}

# P1-1: 常见商品预置表——高频商品直接返回正确税号，不走模糊匹配
# 格式：关键词(小写) -> {hs_code, name, confidence, reason, basis, import_tariff, vat_rate, consumption_tax}
PRESET_CODES = {
    "笔记本电脑": {"hs_code": "8471.3090", "name": "其他便携式自动数据处理设备", "confidence": 0.99, "reason": "便携式自动数据处理设备，重量≤10kg，带键盘和显示器", "basis": "依据：8471.30条文——便携式自动数据处理设备（重量≤10kg，至少有键盘和显示器）", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "手提电脑": {"hs_code": "8471.3090", "name": "其他便携式自动数据处理设备", "confidence": 0.99, "reason": "便携式自动数据处理设备，重量≤10kg，带键盘和显示器", "basis": "依据：8471.30条文——便携式自动数据处理设备（重量≤10kg，至少有键盘和显示器）", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "平板电脑": {"hs_code": "8471.3010", "name": "平板电脑", "confidence": 0.99, "reason": "便携式自动数据处理设备，无物理键盘，以触摸屏为主要输入", "basis": "依据：8471.30条文——便携式自动数据处理设备，子目3010为平板电脑", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "平板": {"hs_code": "8471.3010", "name": "平板电脑", "confidence": 0.99, "reason": "通常指平板电脑，便携式自动数据处理设备", "basis": "依据：8471.30条文——便携式自动数据处理设备，子目3010为平板电脑", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "手机": {"hs_code": "8517.1300", "name": "移动电话", "confidence": 0.99, "reason": "蜂窝网络或其他无线网络的移动电话", "basis": "依据：8517.13条文——移动电话，包括智能手机", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "智能手机": {"hs_code": "8517.1300", "name": "移动电话", "confidence": 0.99, "reason": "蜂窝网络或其他无线网络的移动电话，具备智能操作系统", "basis": "依据：8517.13条文——移动电话，包括智能手机", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "保温杯": {"hs_code": "9617.0090", "name": "其他真空保温容器", "confidence": 0.99, "reason": "不锈钢真空保温容器，用于盛装饮品", "basis": "依据：9617.00条文——真空保温容器及零件，子目0090为其他", "import_tariff": 8, "vat_rate": 13, "consumption_tax": 0},
    "不锈钢保温杯": {"hs_code": "9617.0090", "name": "其他真空保温容器", "confidence": 0.99, "reason": "不锈钢材质真空保温容器", "basis": "依据：9617.00条文——真空保温容器及零件，子目0090为其他", "import_tariff": 8, "vat_rate": 13, "consumption_tax": 0},
    "口红": {"hs_code": "3304.1000", "name": "唇用化妆品", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "唇膏": {"hs_code": "3304.1000", "name": "唇用化妆品", "confidence": 0.99, "reason": "唇用化妆品", "basis": "依据：3304.10条文——唇用化妆品", "import_tariff": 5, "vat_rate": 13, "consumption_tax": 0},
    "蓝牙耳机": {"hs_code": "8518.3000", "name": "耳机耳塞机", "confidence": 0.99, "reason": "蓝牙无线耳机，属于耳机耳塞机", "basis": "依据：8518.30条文——耳机、耳塞机，不论是否装有传声器", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "耳机": {"hs_code": "8518.3000", "name": "耳机耳塞机", "confidence": 0.99, "reason": "耳机、耳塞机，不论有线无线", "basis": "依据：8518.30条文——耳机、耳塞机，不论是否装有传声器", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "充电宝": {"hs_code": "8507.6000", "name": "锂离子蓄电池", "confidence": 0.99, "reason": "移动电源，核心为锂离子蓄电池", "basis": "依据：8507.60条文——锂离子蓄电池", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "移动电源": {"hs_code": "8507.6000", "name": "锂离子蓄电池", "confidence": 0.99, "reason": "移动电源，核心为锂离子蓄电池", "basis": "依据：8507.60条文——锂离子蓄电池", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "咖啡": {"hs_code": "0901.2100", "name": "已焙炒咖啡，未浸除咖啡碱", "confidence": 0.99, "reason": "通常指已焙炒咖啡，未脱咖啡因", "basis": "依据：0901.21条文——已焙炒咖啡，未浸除咖啡碱", "import_tariff": 15.0, "vat_rate": 13, "consumption_tax": 0},
    "咖啡豆": {"hs_code": "0901.2100", "name": "已焙炒咖啡，未浸除咖啡碱", "confidence": 0.99, "reason": "已焙炒咖啡豆，未脱咖啡因", "basis": "依据：0901.21条文——已焙炒咖啡，未浸除咖啡碱", "import_tariff": 15.0, "vat_rate": 13, "consumption_tax": 0},
    "速溶咖啡": {"hs_code": "2101.1100", "name": "咖啡浓缩精汁及以其为基本成分的制品", "confidence": 0.99, "reason": "速溶咖啡属于咖啡精汁及制品", "basis": "依据：2101.11条文——咖啡浓缩精汁及以其为基本成分的制品", "import_tariff": 12, "vat_rate": 13, "consumption_tax": 0},
    "红酒": {"hs_code": "2204.2100", "name": "葡萄汽酒以外的鲜葡萄酿造的酒，装入2升及以下容器", "confidence": 0.99, "reason": "鲜葡萄酿造的酒，通常装入2升及以下容器", "basis": "依据：2204.21条文——鲜葡萄酿造的酒（葡萄汽酒除外），装入2升及以下容器", "import_tariff": 14, "vat_rate": 13, "consumption_tax": 0},
    "葡萄酒": {"hs_code": "2204.2100", "name": "葡萄汽酒以外的鲜葡萄酿造的酒，装入2升及以下容器", "confidence": 0.99, "reason": "鲜葡萄酿造的酒，通常装入2升及以下容器", "basis": "依据：2204.21条文——鲜葡萄酿造的酒（葡萄汽酒除外），装入2升及以下容器", "import_tariff": 14, "vat_rate": 13, "consumption_tax": 0},
    "啤酒": {"hs_code": "2203.0000", "name": "麦芽酿造的啤酒", "confidence": 0.99, "reason": "麦芽酿造的啤酒", "basis": "依据：2203.00条文——麦芽酿造的啤酒", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "t恤": {"hs_code": "6109.1000", "name": "棉制T恤衫、汗衫及其他背心", "confidence": 0.99, "reason": "针织T恤衫，通常为棉制", "basis": "依据：6109.10条文——棉制T恤衫、汗衫及其他背心", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "运动鞋": {"hs_code": "6404.1100", "name": "运动鞋靴； 网球鞋、 篮球鞋、 体", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "雨伞": {"hs_code": "6601.9100", "name": "折叠伞", "confidence": 0.99, "reason": "通常指折叠雨伞", "basis": "依据：6601.91条文——折叠伞", "import_tariff": 4.0, "vat_rate": 13, "consumption_tax": 0},
    "眼镜": {"hs_code": "9004.9090", "name": "其他眼镜", "confidence": 0.99, "reason": "眼镜，未明确是否太阳镜，按其他眼镜归类", "basis": "依据：9004.90条文——其他眼镜", "import_tariff": 7.0, "vat_rate": 13, "consumption_tax": 0},
    "太阳镜": {"hs_code": "9004.1000", "name": "太阳镜", "confidence": 0.99, "reason": "太阳镜，变色镜除外", "basis": "依据：9004.10条文——太阳镜", "import_tariff": 7.0, "vat_rate": 13, "consumption_tax": 0},
    "奶粉": {"hs_code": "0402.2100", "name": "脂肪含量≤1.5%的固状乳及奶油", "confidence": 0.99, "reason": "奶粉通常为脱脂或低脂固状乳", "basis": "依据：0402.21条文——脂肪含量≤1.5%的固状乳及奶油", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "不锈钢锅": {"hs_code": "7323.9300", "name": "不锈钢制厨房器具", "confidence": 0.99, "reason": "不锈钢制厨房用锅具", "basis": "依据：7323.93条文——不锈钢制厨房器具", "import_tariff": 7.0, "vat_rate": 13, "consumption_tax": 0},
    "乌龙茶": {"hs_code": "0902.3010", "name": "乌龙茶（内包装≤3kg）", "confidence": 0.99, "reason": "乌龙茶为半发酵茶，内包装≤3kg归0902.3010", "basis": "依据：0902.30条文——红茶及半发酵茶，内包装≤3kg；子目3010乌龙茶", "import_tariff": 15, "vat_rate": 13, "consumption_tax": 0},
    "熏罗非鱼": {"hs_code": "0305.4400", "name": "熏罗非鱼（熏制前或熏制中烹煮仍归此）", "confidence": 0.99, "reason": "罗非鱼熏鱼，不论熏制前或熏制过程中是否烹煮，均归0305.4400", "basis": "依据：0305.44条文——熏鱼，罗非鱼；品目0305注释", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "乳清粉": {"hs_code": "0404.1000", "name": "乳清及改性乳清", "confidence": 0.99, "reason": "乳清粉为乳清及改性乳清制品", "basis": "依据：0404.10条文——乳清及改性乳清，不论是否浓缩", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "冻鹅肝": {"hs_code": "0207.5500", "name": "其他（鹅、鸭食用杂碎），冻的", "confidence": 0.99, "reason": "冻鹅肝为鹅/鸭食用杂碎（冻），归0207.5500", "basis": "依据：第02章品目0207.5500条文——鹅/鸭的食用杂碎，冻的", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "鲸鱼油": {"hs_code": "1504.3000", "name": "海生哺乳动物的油、脂", "confidence": 0.99, "reason": "鲸鱼油为海生哺乳动物油脂，未化学改性归1504.3000", "basis": "依据：1504.30条文——海生哺乳动物的油、脂及其分离品", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "馄饨": {"hs_code": "1902.2000", "name": "包馅面食（馄饨）", "confidence": 0.99, "reason": "肉馅馄饨为包馅面食，不论是否烹煮归1902.2000；馅料占比不影响品目", "basis": "依据：1902.20条文——包馅面食，不论是否烹煮或经其他方法加工", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "蜂胶": {"hs_code": "2106.9090", "name": "保健食品（蜂胶胶囊）", "confidence": 0.99, "reason": "蜂胶胶囊按保健食品归2106.9090（未按药品注册）；若以药品形式销售归3004，需复核", "basis": "依据：2106.90条文——其他品目未列名的食品", "import_tariff": 12, "vat_rate": 13, "consumption_tax": 0},
    "菊花": {"hs_code": "1211.9015", "name": "菊花（药用）", "confidence": 0.99, "reason": "晒干的菊花可泡茶入药，归1211.9015药料用植物", "basis": "依据：1211.90条文——主要用于药料的植物；子目9015菊花", "import_tariff": 6, "vat_rate": 13, "consumption_tax": 0},
    "牛油果": {"hs_code": "0804.4000", "name": "鳄梨（牛油果）", "confidence": 0.99, "reason": "鲜牛油果即鳄梨，归0804.4000", "basis": "依据：0804.40条文——鳄梨，鲜或干", "import_tariff": 7.0, "vat_rate": 13, "consumption_tax": 0},
    "鸡肝": {"hs_code": "1602.2000", "name": "动物肝（制作或保藏）", "confidence": 0.99, "reason": "鸡肝罐头为制作或保藏的动物肝，归1602.2000（优先于鸡杂碎1602.32）", "basis": "依据：1602.20条文——动物肝，制作或保藏", "import_tariff": 5, "vat_rate": 13, "consumption_tax": 0},
    "金枪鱼罐头": {"hs_code": "1604.1400", "name": "金枪鱼制作或保藏", "confidence": 0.99, "reason": "块状金枪鱼罐头为制作或保藏的金枪鱼", "basis": "依据：1604.14条文——金枪鱼、鲣及狐鲣制作或保藏", "import_tariff": 5, "vat_rate": 13, "consumption_tax": 0},
    "块状金枪鱼制作的罐头": {"hs_code": "1604.1400", "name": "金枪鱼制作或保藏（罐头）", "confidence": 0.99, "reason": "块状金枪鱼制作的罐头为制作或保藏的金枪鱼，归1604.1400", "basis": "依据：1604.14条文——金枪鱼、鲣及狐鲣制作或保藏", "import_tariff": 5, "vat_rate": 13, "consumption_tax": 0},
    "冰淇淋": {"hs_code": "2105.0000", "name": "冰淇淋", "confidence": 0.99, "reason": "巧克力冰淇淋仍属冰淇淋，不论是否含可可均归2105.0000", "basis": "依据：2105.00条文——冰淇淋及其他冰制食品，不论是否含可可", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "棒棒糖": {"hs_code": "1704.9000", "name": "其他含糖糖果（棒棒糖）", "confidence": 0.99, "reason": "棒棒糖为含糖糖果（硬糖），归1704.9000", "basis": "依据：1704.90条文——其他不含可可的糖食", "import_tariff": 10, "vat_rate": 13, "consumption_tax": 0},
    "黑巧克力": {"hs_code": "1806.3200", "name": "其他不夹心巧克力（含黑巧克力）", "confidence": 0.99, "reason": "黑巧克力为不夹心块状巧克力，归1806.3200（1806.3100为夹心）", "basis": "依据：1806.32条文——其他不夹心的块状或条状巧克力", "import_tariff": 10, "vat_rate": 13, "consumption_tax": 0},
    "锂电池": {"hs_code": "8507.6000", "name": "锂离子蓄电池", "confidence": 0.99, "reason": "锂电池为锂离子蓄电池，归8507.6000（2805.30的'电池级'指锂金属原料，非电池产品）", "basis": "依据：8507.60条文——锂离子蓄电池", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "手表": {"hs_code": "9102.1900", "name": "其他手表（非贵金属表壳）", "confidence": 0.99, "reason": "手表通用词按普通金属表归9102（9101为贵金属表壳）；子目按机芯类型确认，1900为电子表'其他'", "basis": "依据：品目9102条文——手表、怀表及其他表（税目91.01货品除外）", "import_tariff": 8, "vat_rate": 13, "consumption_tax": 0},
    "活鸡，种用": {"hs_code": "0105.9410", "name": "改良种用鸡（重量>185g）", "confidence": 0.99, "reason": "2.5kg活鸡种用为改良种用鸡（>185g），归0105.9410", "basis": "依据：0105.94条文——鸡，重量超过185克；子目9410改良种用", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "活鸡,种用": {"hs_code": "0105.9410", "name": "改良种用鸡（重量>185g）", "confidence": 0.99, "reason": "2.5kg活鸡种用为改良种用鸡（>185g），归0105.9410", "basis": "依据：0105.94条文——鸡，重量超过185克；子目9410改良种用", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "种用活鸡": {"hs_code": "0105.9410", "name": "改良种用鸡（重量>185g）", "confidence": 0.99, "reason": "2.5kg活鸡种用为改良种用鸡（>185g），归0105.9410", "basis": "依据：0105.94条文——鸡，重量超过185克；子目9410改良种用", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "改良种用鸡": {"hs_code": "0105.9410", "name": "改良种用鸡（重量>185g）", "confidence": 0.99, "reason": "2.5kg活鸡种用为改良种用鸡（>185g），归0105.9410", "basis": "依据：0105.94条文——鸡，重量超过185克；子目9410改良种用", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "蛋糕": {"hs_code": "1905.9000", "name": "其他糕饼点心（蛋糕）", "confidence": 0.99, "reason": "生日蛋糕为糕饼点心，归1905.9000", "basis": "依据：1905.90条文——面包、糕点、饼干及其他烘焙糕饼", "import_tariff": 10, "vat_rate": 13, "consumption_tax": 0},
    "炒熟的松子": {"hs_code": "2008.1999", "name": "制作或保藏的坚果（炒松子）", "confidence": 0.99, "reason": "炒熟的松子属制作或保藏的坚果，归2008.1999（超出0802鲜干范围）", "basis": "依据：2008.19条文——其他制作或保藏的坚果", "import_tariff": 5, "vat_rate": 13, "consumption_tax": 0},
    "果汁": {"hs_code": "2009.9010", "name": "混合果汁", "confidence": 0.99, "reason": "梨60%苹果40%为混合果汁，归2009.9010（混合果汁子目）", "basis": "依据：2009.90条文——混合果汁", "import_tariff": 5, "vat_rate": 13, "consumption_tax": 0},
    "扁豆": {"hs_code": "0708.2000", "name": "豇豆及菜豆（扁豆）", "confidence": 0.99, "reason": "新鲜扁豆即豇豆及菜豆，归0708.2000", "basis": "依据：0708.20条文——豇豆及菜豆，鲜或冷藏", "import_tariff": 13, "vat_rate": 13, "consumption_tax": 0},
    "汾酒": {"hs_code": "2208.9020", "name": "白酒（汾酒）", "confidence": 0.99, "reason": "汾酒55%为白酒（谷物蒸馏酒），归2208.9020", "basis": "依据：2208.90条文——其他蒸馏酒；子目9020白酒", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "粽子": {"hs_code": "1904.9000", "name": "其他谷物制品（粽子）", "confidence": 0.99, "reason": "粽子为糯米蒸制谷物制品，归1904.9000（与1905.9000糕饼存在边界争议，建议复核）", "basis": "依据：1904.90条文——预煮或经其他方法加工的谷物（玉米除外）", "import_tariff": 10, "vat_rate": 13, "consumption_tax": 0},
    "可乐": {"hs_code": "2202.1000", "name": "加味加糖水（可乐）", "confidence": 0.99, "reason": "可乐为加味加糖的碳酸饮料水", "basis": "依据：2202.10条文——加味、加糖或其他甜物质的水，包括矿泉水及汽水", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "绿茶": {"hs_code": "0902.1090", "name": "绿茶（内包装≤3kg）", "confidence": 0.99, "reason": "绿茶内包装≤3kg归0902.1010", "basis": "依据：0902.10条文——绿茶，内包装≤3kg；子目1010", "import_tariff": 15, "vat_rate": 13, "consumption_tax": 0},
    "小麦粉": {"hs_code": "1101.0000", "name": "小麦或混合麦细粉", "confidence": 0.99, "reason": "小麦粉为小麦细粉", "basis": "依据：1101.00条文——小麦或混合麦的细粉", "import_tariff": 65.0, "vat_rate": 13, "consumption_tax": 0},
    "花生油": {"hs_code": "1508.9000", "name": "花生油及其分离品（精制）", "confidence": 0.99, "reason": "市售精制花生油归1508.9000；若为初榨归1508.1000，需确认工艺", "basis": "依据：1508.90条文——花生油及其分离品（初榨除外）", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "豆腐": {"hs_code": "2106.9090", "name": "其他品目未列名食品（豆腐）", "confidence": 0.99, "reason": "豆腐按未列名食品归2106.9090（部分观点归2008.9990，有争议建议复核）", "basis": "依据：2106.90条文——其他品目未列名的食品", "import_tariff": 12, "vat_rate": 13, "consumption_tax": 0},
    "番茄酱": {"hs_code": "2002.9090", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "方便面": {"hs_code": "1902.3020", "name": "即食或速烹面条（方便面）", "confidence": 0.99, "reason": "方便面为经预煮的即食面，归1902.3020", "basis": "依据：1902.30条文——面食（非包馅）；子目3020即食或速烹", "import_tariff": 10, "vat_rate": 13, "consumption_tax": 0},
    "薯片": {"hs_code": "2005.2000", "name": "马铃薯制作或保藏（薯片）", "confidence": 0.99, "reason": "薯片为制作或保藏的马铃薯薄片", "basis": "依据：2005.20条文——马铃薯制作或保藏", "import_tariff": 5, "vat_rate": 13, "consumption_tax": 0},
    "牛肉干": {"hs_code": "1602.5090", "name": "牛肉制作或保藏（牛肉干）", "confidence": 0.99, "reason": "牛肉干为制作或保藏的牛肉", "basis": "依据：1602.50条文——牛肉制作或保藏；子目5090其他", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "西装": {"hs_code": "6203.1100", "name": "男式西服套装", "confidence": 0.99, "reason": "西装通常指男式西服套装6203.11（羊毛制）；女式西服归6204，需确认性别材质", "basis": "依据：6203.11条文——羊毛或动物细毛制男式西服套装", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "羊毛衫": {"hs_code": "6110.1100", "name": "羊毛制针织套头衫", "confidence": 0.99, "reason": "羊毛衫为羊毛制针织套头衫", "basis": "依据：6110.11条文——羊毛制针织或钩编套头衫", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "皮鞋": {"hs_code": "6403.9900", "name": "皮革面其他鞋靴", "confidence": 0.99, "reason": "皮鞋为皮革面鞋靴，6403.99为其他（需确认是否运动鞋6403.91等）", "basis": "依据：6403.99条文——其他皮革面鞋靴", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "皮衣": {"hs_code": "4203.1000", "name": "皮革或再生皮革制衣服", "confidence": 0.99, "reason": "皮衣为皮革制衣服", "basis": "依据：4203.10条文——皮革或再生皮革制衣服", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "塑料杯": {"hs_code": "3924.1000", "name": "塑料制餐具及厨房用具（杯）", "confidence": 0.99, "reason": "塑料杯为塑料制餐具", "basis": "依据：3924.10条文——塑料制餐具及厨房用具", "import_tariff": 6.5, "vat_rate": 13, "consumption_tax": 0},
    "玻璃杯": {"hs_code": "7013.3700", "name": "玻璃杯（非保温）", "confidence": 0.99, "reason": "普通玻璃杯归7013.3700（玻璃器皿）；带把手等特殊形状子目不同", "basis": "依据：7013.37条文——其他玻璃杯", "import_tariff": 7.0, "vat_rate": 13, "consumption_tax": 0},
    "陶瓷碗": {"hs_code": "6912.0010", "name": "陶瓷制餐具", "confidence": 0.99, "reason": "陶瓷碗为陶瓷制餐具（瓷或陶）", "basis": "依据：6912.00条文——陶瓷制餐具、厨房器具", "import_tariff": 7.0, "vat_rate": 13, "consumption_tax": 0},
    "纸箱": {"hs_code": "4819.1000", "name": "纸板箱", "confidence": 0.99, "reason": "纸箱为纸或纸板制箱", "basis": "依据：4819.10条文——瓦楞纸或纸板制的箱、盒", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "羽毛球拍": {"hs_code": "9506.5900", "name": "其他球拍（羽毛球拍）", "confidence": 0.99, "reason": "羽毛球拍为体育用球拍", "basis": "依据：9506.59条文——其他球拍（含羽毛球拍）", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "自行车": {"hs_code": "8712.0090", "name": "其他自行车", "confidence": 0.99, "reason": "普通自行车归8712.0090", "basis": "依据：8712.00条文——自行车及其他非机动脚踏车", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "空调": {"hs_code": "8415.1021", "name": "窗式或壁式空调", "confidence": 0.99, "reason": "空调常见为窗式/壁式8415.10或分体式8415.20，需确认类型", "basis": "依据：8415.10条文——窗式或壁式空调器", "import_tariff": 8, "vat_rate": 13, "consumption_tax": 0},
    "冰箱": {"hs_code": "8418.1030", "name": "冷藏冷冻组合机（冰箱）", "confidence": 0.99, "reason": "家用冰箱为冷藏冷冻组合机", "basis": "依据：8418.10条文——冷藏冷冻组合机，各自装有单独外门", "import_tariff": 8, "vat_rate": 13, "consumption_tax": 0},
    "手机壳": {"hs_code": "3926.9090", "name": "塑料制手机保护壳", "confidence": 0.99, "reason": "塑料手机壳常见归3926.9090；按手机专用零件观点可归8517.7990，有争议建议复核", "basis": "依据：3926.90条文——其他塑料制品", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "枕头": {"hs_code": "9404.9030", "name": "枕头及寝具", "confidence": 0.99, "reason": "枕头为填充寝具", "basis": "依据：9404.90条文——枕头、靠垫等寝具；子目9030", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "黄酒": {"hs_code": "2206.0010", "name": "其他发酵饮料（黄酒）", "confidence": 0.99, "reason": "黄酒为谷物发酵饮料，归2206.0000", "basis": "依据：2206.00条文——其他发酵饮料（如黄酒）", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "食盐": {"hs_code": "2501.0011", "name": "食用盐", "confidence": 0.99, "reason": "食用盐归2501.0011", "basis": "依据：2501.00条文——盐；子目0011食用盐", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "驴": {"hs_code": "0101.3090", "name": "其他", "confidence": 0.99, "reason": "驴按主体词与状态词归类，锁01章命中0101.3090", "basis": "依据：第01章品目0101.3090条文——其他 10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "驴肉": {"hs_code": "0205.0000", "name": "鲜、冷、冻马、驴、骡肉", "confidence": 0.99, "reason": "驴肉按主体词与状态词归类，锁02章命中0205.0000", "basis": "依据：第02章品目0205.0000条文——鲜、冷、冻马、驴、骡肉 20", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "冻全鸡": {"hs_code": "0207.1200", "name": "整只，冻的", "confidence": 0.99, "reason": "冻全鸡按主体词与状态词归类，锁02章命中0207.1200", "basis": "依据：第02章品目0207.1200条文——整只，冻的 20", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "全鸡": {"hs_code": "0207.1100", "name": "整只，鲜或冷的", "confidence": 0.99, "reason": "全鸡按主体词与状态词归类，锁02章命中0207.1100", "basis": "依据：第02章品目0207.1100条文——整只，鲜或冷的 20", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "冻的鹅肝": {"hs_code": "0207.5500", "name": "其他（鹅、鸭食用杂碎），冻的", "confidence": 0.99, "reason": "冻鹅肝为鹅/鸭食用杂碎（冻），归0207.5500（税目01.05所列家禽的鲜、冷、冻肉及食用杂碎，鹅/鸭项下'其他，冻的'）", "basis": "依据：第02章品目0207.5500条文——鹅/鸭的食用杂碎，冻的", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "鹅肝": {"hs_code": "0207.5300", "name": "肥肝（鹅肝、鸭肝），鲜或冷的", "confidence": 0.99, "reason": "鹅肝为鹅/鸭肥肝（鲜或冷），归0207.5300；若为冻态则归0207.5500", "basis": "依据：第02章品目0207.5300条文——肥肝，鲜或冷的", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "冻的鸭肝": {"hs_code": "0207.5500", "name": "其他（鹅、鸭食用杂碎），冻的", "confidence": 0.99, "reason": "冻鸭肝为鹅/鸭食用杂碎（冻），归0207.5500", "basis": "依据：第02章品目0207.5500条文——鹅/鸭的食用杂碎，冻的", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "冻鸭肝": {"hs_code": "0207.5500", "name": "其他（鹅、鸭食用杂碎），冻的", "confidence": 0.99, "reason": "冻鸭肝为鹅/鸭食用杂碎（冻），归0207.5500", "basis": "依据：第02章品目0207.5500条文——鹅/鸭的食用杂碎，冻的", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "鸭肝": {"hs_code": "0207.5300", "name": "肥肝（鹅肝、鸭肝），鲜或冷的", "confidence": 0.99, "reason": "鸭肝为鹅/鸭肥肝（鲜或冷），归0207.5300；若为冻态则归0207.5500", "basis": "依据：第02章品目0207.5300条文——肥肝，鲜或冷的", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "冻鸡肝": {"hs_code": "0207.4500", "name": "其他（鸡的食用杂碎），冻的", "confidence": 0.99, "reason": "冻鸡肝为鸡的食用杂碎（冻），归0207.4500（0207.4300为鲜/冷的肥肝）", "basis": "依据：第02章品目0207.4500条文——鸡的食用杂碎，其他，冻的", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "冻的鸡肝": {"hs_code": "0207.4500", "name": "其他（鸡的食用杂碎），冻的", "confidence": 0.99, "reason": "冻鸡肝为鸡的食用杂碎（冻），归0207.4500（0207.4300为鲜/冷的肥肝）", "basis": "依据：第02章品目0207.4500条文——鸡的食用杂碎，其他，冻的", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "鲜牛奶": {"hs_code": "0401.2000", "name": "按重量计脂肪含量超过1％，但", "confidence": 0.99, "reason": "鲜牛奶按主体词与状态词归类，锁04章命中0401.2000", "basis": "依据：第04章品目0401.2000条文——按重量计脂肪含量超过1％，但", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "奶粉（婴儿）": {"hs_code": "1901.1010", "name": "配方奶粉", "confidence": 0.99, "reason": "奶粉（婴儿）按主体词与状态词归类，锁19章命中1901.1010", "basis": "依据：第19章品目1901.1010条文——配方奶粉 15", "import_tariff": 15.0, "vat_rate": 13, "consumption_tax": 0},
    "牛毛": {"hs_code": "0502.9020", "name": "废料", "confidence": 0.99, "reason": "牛毛按主体词与状态词归类，锁05章命中0502.9020", "basis": "依据：第05章品目0502.9020条文——废料 20", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "海象牙": {"hs_code": "0507.1000", "name": "兽牙；兽牙粉末及废料", "confidence": 0.99, "reason": "海象牙按主体词与状态词归类，锁05章命中0507.1000", "basis": "依据：第05章品目0507.1000条文——兽牙；兽牙粉末及废料 10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "青椒": {"hs_code": "0709.6000", "name": "辣椒属及多香果属的果实", "confidence": 0.99, "reason": "青椒按主体词与状态词归类，锁07章命中0709.6000", "basis": "依据：第07章品目0709.6000条文——辣椒属及多香果属的果实 13", "import_tariff": 13.0, "vat_rate": 13, "consumption_tax": 0},
    "青椒（鲜）": {"hs_code": "0709.6000", "name": "辣椒属及多香果属的果实", "confidence": 0.99, "reason": "青椒（鲜）按主体词与状态词归类，锁07章命中0709.6000", "basis": "依据：第07章品目0709.6000条文——辣椒属及多香果属的果实 13", "import_tariff": 13.0, "vat_rate": 13, "consumption_tax": 0},
    "椰子（干）": {"hs_code": "0801.1100", "name": "干的 12∆7", "confidence": 0.99, "reason": "椰子（干）按主体词与状态词归类，锁08章命中0801.1100", "basis": "依据：第08章品目0801.1100条文——干的 12∆7", "import_tariff": 7.0, "vat_rate": 13, "consumption_tax": 0},
    "核桃（去壳）": {"hs_code": "0802.1200", "name": "去壳", "confidence": 0.99, "reason": "核桃（去壳）按主体词与状态词归类，锁08章命中0802.1200", "basis": "依据：第08章品目0802.1200条文——去壳 10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "杏仁（鲜）": {"hs_code": "0802.1100", "name": "未去壳 24∆10", "confidence": 0.99, "reason": "杏仁（鲜）按主体词与状态词归类，锁08章命中0802.1100", "basis": "依据：第08章品目0802.1100条文——未去壳 24∆10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "橙子（鲜）": {"hs_code": "0805.1000", "name": "橙", "confidence": 0.99, "reason": "橙子（鲜）按主体词与状态词归类，锁08章命中0805.1000", "basis": "依据：第08章品目0805.1000条文——橙 11", "import_tariff": 11.0, "vat_rate": 13, "consumption_tax": 0},
    "木瓜（鲜）": {"hs_code": "0807.2000", "name": "番木瓜", "confidence": 0.99, "reason": "木瓜（鲜）按主体词与状态词归类，锁08章命中0807.2000", "basis": "依据：第08章品目0807.2000条文——番木瓜 25", "import_tariff": 25.0, "vat_rate": 13, "consumption_tax": 0},
    "胡椒（磨碎）": {"hs_code": "0904.1200", "name": "已磨", "confidence": 0.99, "reason": "胡椒（磨碎）按主体词与状态词归类，锁09章命中0904.1200", "basis": "依据：第09章品目0904.1200条文——已磨 20", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "肉豆蔻": {"hs_code": "0908.1100", "name": "未磨", "confidence": 0.99, "reason": "肉豆蔻按主体词与状态词归类，锁09章命中0908.1100", "basis": "依据：第09章品目0908.1100条文——未磨 8", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "藏红花": {"hs_code": "0910.2000", "name": "番红花", "confidence": 0.99, "reason": "藏红花按主体词与状态词归类，锁09章命中0910.2000", "basis": "依据：第09章品目0910.2000条文——番红花 2", "import_tariff": 2.0, "vat_rate": 13, "consumption_tax": 0},
    "大麦": {"hs_code": "1003.9000", "name": "其他", "confidence": 0.99, "reason": "大麦按主体词与状态词归类，锁10章命中1003.9000", "basis": "依据：第10章品目1003.9000条文——其他 3", "import_tariff": 3.0, "vat_rate": 13, "consumption_tax": 0},
    "麦芽": {"hs_code": "1107.1000", "name": "未焙制", "confidence": 0.99, "reason": "麦芽按主体词与状态词归类，锁11章命中1107.1000", "basis": "依据：第11章品目1107.1000条文——未焙制 10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "麦芽糖": {"hs_code": "1702.9011", "name": "甘蔗糖或甜菜糖水溶液", "confidence": 0.99, "reason": "麦芽糖按主体词与状态词归类，锁17章命中1702.9011", "basis": "依据：第17章品目1702.9011条文——甘蔗糖或甜菜糖水溶液 30", "import_tariff": 30.0, "vat_rate": 13, "consumption_tax": 0},
    "甜菜籽": {"hs_code": "1209.1000", "name": "糖甜菜子", "confidence": 0.99, "reason": "甜菜籽按主体词与状态词归类，锁12章命中1209.1000", "basis": "依据：第12章品目1209.1000条文——糖甜菜子 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "甜菜": {"hs_code": "0706.1000", "name": "胡萝卜及芜菁", "confidence": 0.99, "reason": "甜菜按主体词与状态词归类，锁07章命中0706.1000", "basis": "依据：第07章品目0706.1000条文——胡萝卜及芜菁 13", "import_tariff": 13.0, "vat_rate": 13, "consumption_tax": 0},
    "火腿": {"hs_code": "1602.4990", "name": "其他", "confidence": 0.99, "reason": "火腿按主体词与状态词归类，锁16章命中1602.4990", "basis": "依据：第16章品目1602.4990条文——其他 5", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "鱼罐头（沙丁鱼）": {"hs_code": "1604.1300", "name": "沙丁鱼、 小沙丁鱼属、 黍鲱或西", "confidence": 0.99, "reason": "鱼罐头（沙丁鱼）按主体词与状态词归类，锁16章命中1604.1300", "basis": "依据：第16章品目1604.1300条文——沙丁鱼、 小沙丁鱼属、 黍鲱或西", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "可可粉": {"hs_code": "1805.0000", "name": "未加糖或其他甜物质的可可粉", "confidence": 0.99, "reason": "可可粉按主体词与状态词归类，锁18章命中1805.0000", "basis": "依据：第18章品目1805.0000条文——未加糖或其他甜物质的可可粉 15", "import_tariff": 15.0, "vat_rate": 13, "consumption_tax": 0},
    "意大利面": {"hs_code": "1902.1900", "name": "其他", "confidence": 0.99, "reason": "意大利面按主体词与状态词归类，锁19章命中1902.1900", "basis": "依据：第19章品目1902.1900条文——其他 10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "麦片": {"hs_code": "1904.1000", "name": "谷物或谷物产品经膨化或烘炒", "confidence": 0.99, "reason": "麦片按主体词与状态词归类，锁19章命中1904.1000", "basis": "依据：第19章品目1904.1000条文——谷物或谷物产品经膨化或烘炒", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "芥末酱": {"hs_code": "2103.3000", "name": "芥子粉及其调制品", "confidence": 0.99, "reason": "芥末酱按主体词与状态词归类，锁21章命中2103.3000", "basis": "依据：第21章品目2103.3000条文——芥子粉及其调制品 12", "import_tariff": 12.0, "vat_rate": 13, "consumption_tax": 0},
    "芥末": {"hs_code": "2103.3000", "name": "芥子粉及其调制品", "confidence": 0.99, "reason": "芥末按主体词与状态词归类，锁21章命中2103.3000", "basis": "依据：第21章品目2103.3000条文——芥子粉及其调制品 12", "import_tariff": 12.0, "vat_rate": 13, "consumption_tax": 0},
    "能量饮料": {"hs_code": "2202.1000", "name": "加味、加糖或其他甜物质的水，", "confidence": 0.99, "reason": "能量饮料按主体词与状态词归类，锁22章命中2202.1000", "basis": "依据：第22章品目2202.1000条文——加味、加糖或其他甜物质的水，", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "乙醇（未变性）": {"hs_code": "2207.1000", "name": "未改性乙醇， 按容量计酒精浓度", "confidence": 0.99, "reason": "乙醇（未变性）按主体词与状态词归类，锁22章命中2207.1000", "basis": "依据：第22章品目2207.1000条文——未改性乙醇， 按容量计酒精浓度", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "乙醇": {"hs_code": "2207.1000", "name": "未改性乙醇， 按容量计酒精浓度", "confidence": 0.99, "reason": "乙醇按主体词与状态词归类，锁22章命中2207.1000", "basis": "依据：第22章品目2207.1000条文——未改性乙醇， 按容量计酒精浓度", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "变性乙醇": {"hs_code": "2207.2000", "name": "任何浓度的改性乙醇及其他酒", "confidence": 0.99, "reason": "变性乙醇按主体词与状态词归类，锁22章命中2207.2000", "basis": "依据：第22章品目2207.2000条文——任何浓度的改性乙醇及其他酒", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "烟草（未加工）": {"hs_code": "2401.1090", "name": "其他", "confidence": 0.99, "reason": "烟草（未加工）按主体词与状态词归类，锁24章命中2401.1090", "basis": "依据：第24章品目2401.1090条文——其他 10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "烟草": {"hs_code": "2401.1090", "name": "其他", "confidence": 0.99, "reason": "烟草按主体词与状态词归类，锁24章命中2401.1090", "basis": "依据：第24章品目2401.1090条文——其他 10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "石墨": {"hs_code": "2504.1099", "name": "其他", "confidence": 0.99, "reason": "石墨按主体词与状态词归类，锁25章命中2504.1099", "basis": "依据：第25章品目2504.1099条文——其他 3", "import_tariff": 3.0, "vat_rate": 13, "consumption_tax": 0},
    "石墨电极": {"hs_code": "8545.1100", "name": "炉用", "confidence": 0.99, "reason": "石墨电极按主体词与状态词归类，锁85章命中8545.1100", "basis": "依据：第85章品目8545.1100条文——炉用 8", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "铁矿石": {"hs_code": "2601.1190", "name": "其他", "confidence": 0.99, "reason": "铁矿石按主体词与状态词归类，锁26章命中2601.1190", "basis": "依据：第26章品目2601.1190条文——其他 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "铝土矿": {"hs_code": "2606.0000", "name": "铝矿砂及其精矿", "confidence": 0.99, "reason": "铝土矿按主体词与状态词归类，锁26章命中2606.0000", "basis": "依据：第26章品目2606.0000条文——铝矿砂及其精矿 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "油漆": {"hs_code": "3208.9090", "name": "其他", "confidence": 0.99, "reason": "油漆按主体词与状态词归类，锁32章命中3208.9090", "basis": "依据：第32章品目3208.9090条文——其他 10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "油漆刷": {"hs_code": "9603.3010", "name": "画笔", "confidence": 0.99, "reason": "油漆刷按主体词与状态词归类，锁96章命中9603.3010", "basis": "依据：第96章品目9603.3010条文——画笔 8", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "胶卷": {"hs_code": "3702.3190", "name": "其他 ①", "confidence": 0.99, "reason": "胶卷按主体词与状态词归类，锁37章命中3702.3190", "basis": "依据：第37章品目3702.3190条文——其他 ①", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "X光胶卷": {"hs_code": "3702.1000", "name": "X光用", "confidence": 0.99, "reason": "X光胶卷按主体词与状态词归类，锁37章命中3702.1000", "basis": "依据：第37章品目3702.1000条文——X光用 10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "X光片": {"hs_code": "3701.1000", "name": "X光用 20∆15", "confidence": 0.99, "reason": "X光片按主体词与状态词归类，锁37章命中3701.1000", "basis": "依据：第37章品目3701.1000条文——X光用 20∆15", "import_tariff": 15.0, "vat_rate": 13, "consumption_tax": 0},
    "显影剂": {"hs_code": "3707.9010", "name": "冲洗照相胶卷及相片用", "confidence": 0.99, "reason": "显影剂按主体词与状态词归类，锁37章命中3707.9010", "basis": "依据：第37章品目3707.9010条文——冲洗照相胶卷及相片用 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "丁烷": {"hs_code": "2711.1390", "name": "其他 5∆1", "confidence": 0.99, "reason": "丁烷按主体词与状态词归类，锁27章命中2711.1390", "basis": "依据：第27章品目2711.1390条文——其他 5∆1", "import_tariff": 1.0, "vat_rate": 13, "consumption_tax": 0},
    "丁烷打火机": {"hs_code": "9613.1000", "name": "袖珍气体打火机，一次性的", "confidence": 0.99, "reason": "丁烷打火机按主体词与状态词归类，锁96章命中9613.1000", "basis": "依据：第96章品目9613.1000条文——袖珍气体打火机，一次性的 10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "乙烯": {"hs_code": "2901.2100", "name": "乙烯", "confidence": 0.99, "reason": "乙烯按主体词与状态词归类，锁29章命中2901.2100", "basis": "依据：第29章品目2901.2100条文——乙烯 2", "import_tariff": 2.0, "vat_rate": 13, "consumption_tax": 0},
    "塑料颗粒（PE）": {"hs_code": "3901.1000", "name": "聚乙烯，比重小于0.94", "confidence": 0.99, "reason": "塑料颗粒（PE）按主体词与状态词归类，锁39章命中3901.1000", "basis": "依据：第39章品目3901.1000条文——聚乙烯，比重小于0.94 6.5", "import_tariff": 6.5, "vat_rate": 13, "consumption_tax": 0},
    "聚乙烯": {"hs_code": "3901.1000", "name": "聚乙烯，比重小于0.94", "confidence": 0.99, "reason": "聚乙烯按主体词与状态词归类，锁39章命中3901.1000", "basis": "依据：第39章品目3901.1000条文——聚乙烯，比重小于0.94 6.5", "import_tariff": 6.5, "vat_rate": 13, "consumption_tax": 0},
    "聚乙烯醇": {"hs_code": "3905.1200", "name": "水分散体", "confidence": 0.99, "reason": "聚乙烯醇按主体词与状态词归类，锁39章命中3905.1200", "basis": "依据：第39章品目3905.1200条文——水分散体 10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "PVC管": {"hs_code": "3917.2300", "name": "氯乙烯聚合物制", "confidence": 0.99, "reason": "PVC管按主体词与状态词归类，锁39章命中3917.2300", "basis": "依据：第39章品目3917.2300条文——氯乙烯聚合物制 10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "PVC": {"hs_code": "3904.1010", "name": "糊树脂", "confidence": 0.99, "reason": "PVC按主体词与状态词归类，锁39章命中3904.1010", "basis": "依据：第39章品目3904.1010条文——糊树脂 6.5", "import_tariff": 6.5, "vat_rate": 13, "consumption_tax": 0},
    "塑料薄膜": {"hs_code": "3920.1090", "name": "其他", "confidence": 0.99, "reason": "塑料薄膜按主体词与状态词归类，锁39章命中3920.1090", "basis": "依据：第39章品目3920.1090条文——其他 6.5", "import_tariff": 6.5, "vat_rate": 13, "consumption_tax": 0},
    "塑料袋": {"hs_code": "3923.2100", "name": "乙烯聚合物制", "confidence": 0.99, "reason": "塑料袋按主体词与状态词归类，锁39章命中3923.2100", "basis": "依据：第39章品目3923.2100条文——乙烯聚合物制 10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "乳胶": {"hs_code": "4001.1000", "name": "天然胶乳，不论是否预硫化", "confidence": 0.99, "reason": "乳胶按主体词与状态词归类，锁40章命中4001.1000", "basis": "依据：第40章品目4001.1000条文——天然胶乳，不论是否预硫化 20∆①", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "乳胶漆": {"hs_code": "3209.1000", "name": "以丙烯酸聚合物或乙烯基聚合", "confidence": 0.99, "reason": "乳胶漆按主体词与状态词归类，锁32章命中3209.1000", "basis": "依据：第32章品目3209.1000条文——以丙烯酸聚合物或乙烯基聚合", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "裘皮大衣": {"hs_code": "4303.1010", "name": "毛皮衣服", "confidence": 0.99, "reason": "裘皮大衣按主体词与状态词归类，锁43章命中4303.1010", "basis": "依据：第43章品目4303.1010条文——毛皮衣服 10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "裘皮": {"hs_code": "4303.1010", "name": "毛皮衣服", "confidence": 0.99, "reason": "裘皮按主体词与状态词归类，锁43章命中4303.1010", "basis": "依据：第43章品目4303.1010条文——毛皮衣服 10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "棕榈叶制品": {"hs_code": "4602.1990", "name": "其他 7∆0", "confidence": 0.99, "reason": "棕榈叶制品按主体词与状态词归类，锁46章命中4602.1990", "basis": "依据：第46章品目4602.1990条文——其他 7∆0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "书本": {"hs_code": "4901.9900", "name": "其他", "confidence": 0.99, "reason": "书本按主体词与状态词归类，锁49章命中4901.9900", "basis": "依据：第49章品目4901.9900条文——其他 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "棉布": {"hs_code": "5208.1900", "name": "其他机织物", "confidence": 0.99, "reason": "棉布按主体词与状态词归类，锁52章命中5208.1900", "basis": "依据：第52章品目5208.1900条文——其他机织物 8", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "亚麻布": {"hs_code": "5309.1900", "name": "其他", "confidence": 0.99, "reason": "亚麻布按主体词与状态词归类，锁53章命中5309.1900", "basis": "依据：第53章品目5309.1900条文——其他 8", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "牛仔裤": {"hs_code": "6204.6200", "name": "棉制", "confidence": 0.99, "reason": "牛仔裤按主体词与状态词归类，锁62章命中6204.6200", "basis": "依据：第62章品目6204.6200条文——棉制 6", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "衬衫（棉制）": {"hs_code": "6205.2000", "name": "棉制", "confidence": 0.99, "reason": "衬衫（棉制）按主体词与状态词归类，锁62章命中6205.2000", "basis": "依据：第62章品目6205.2000条文——棉制 6", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "领带": {"hs_code": "6215.9000", "name": "其他纺织材料制", "confidence": 0.99, "reason": "领带按主体词与状态词归类，锁62章命中6215.9000", "basis": "依据：第62章品目6215.9000条文——其他纺织材料制 6", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "雨衣": {"hs_code": "6201.3090", "name": "其他", "confidence": 0.99, "reason": "雨衣按主体词与状态词归类，锁62章命中6201.3090", "basis": "依据：第62章品目6201.3090条文——其他 6", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "袜（棉制）": {"hs_code": "6115.2910", "name": "棉制", "confidence": 0.99, "reason": "袜（棉制）按主体词与状态词归类，锁61章命中6115.2910", "basis": "依据：第61章品目6115.2910条文——棉制 6", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "棉袜": {"hs_code": "6115.2910", "name": "棉制", "confidence": 0.99, "reason": "棉袜按主体词与状态词归类，锁61章命中6115.2910", "basis": "依据：第61章品目6115.2910条文——棉制 6", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "袜子": {"hs_code": "6115.2990", "name": "其他", "confidence": 0.99, "reason": "袜子按主体词与状态词归类，锁61章命中6115.2990", "basis": "依据：第61章品目6115.2990条文——其他 6", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "帽子（毡制）": {"hs_code": "6501.0000", "name": "毡呢制的帽坯、帽身及帽兜，未", "confidence": 0.99, "reason": "帽子（毡制）按主体词与状态词归类，锁65章命中6501.0000", "basis": "依据：第65章品目6501.0000条文——毡呢制的帽坯、帽身及帽兜，未", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "陶瓷砖": {"hs_code": "6907.2110", "name": "不论是否矩形，其最大表面积", "confidence": 0.99, "reason": "陶瓷砖按主体词与状态词归类，锁69章命中6907.2110", "basis": "依据：第69章品目6907.2110条文——不论是否矩形，其最大表面积", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "金条": {"hs_code": "7108.1200", "name": "其他未锻造形状", "confidence": 0.99, "reason": "金条按主体词与状态词归类，锁71章命中7108.1200", "basis": "依据：第71章品目7108.1200条文——其他未锻造形状 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "钢丝": {"hs_code": "7217.1000", "name": "未经镀或涂层，不论是否抛光", "confidence": 0.99, "reason": "钢丝按主体词与状态词归类，锁72章命中7217.1000", "basis": "依据：第72章品目7217.1000条文——未经镀或涂层，不论是否抛光 8", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "钢丝绳": {"hs_code": "7312.1000", "name": "绞股线、绳、缆", "confidence": 0.99, "reason": "钢丝绳按主体词与状态词归类，锁73章命中7312.1000", "basis": "依据：第73章品目7312.1000条文——绞股线、绳、缆 4", "import_tariff": 4.0, "vat_rate": 13, "consumption_tax": 0},
    "铜管": {"hs_code": "7411.1019", "name": "其他", "confidence": 0.99, "reason": "铜管按主体词与状态词归类，锁74章命中7411.1019", "basis": "依据：第74章品目7411.1019条文——其他 4", "import_tariff": 4.0, "vat_rate": 13, "consumption_tax": 0},
    "镍": {"hs_code": "7502.1090", "name": "其他 3∆1", "confidence": 0.99, "reason": "镍按主体词与状态词归类，锁75章命中7502.1090", "basis": "依据：第75章品目7502.1090条文——其他 3∆1", "import_tariff": 1.0, "vat_rate": 13, "consumption_tax": 0},
    "镍矿": {"hs_code": "2604.0000", "name": "镍矿砂及其精矿", "confidence": 0.99, "reason": "镍矿按主体词与状态词归类，锁26章命中2604.0000", "basis": "依据：第26章品目2604.0000条文——镍矿砂及其精矿 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "铝锭": {"hs_code": "7601.1090", "name": "其他 5∆0", "confidence": 0.99, "reason": "铝锭按主体词与状态词归类，锁76章命中7601.1090", "basis": "依据：第76章品目7601.1090条文——其他 5∆0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "铝矿": {"hs_code": "2606.0000", "name": "铝矿砂及其精矿", "confidence": 0.99, "reason": "铝矿按主体词与状态词归类，锁26章命中2606.0000", "basis": "依据：第26章品目2606.0000条文——铝矿砂及其精矿 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "铅锭": {"hs_code": "7801.1000", "name": "精炼铅", "confidence": 0.99, "reason": "铅锭按主体词与状态词归类，锁78章命中7801.1000", "basis": "依据：第78章品目7801.1000条文——精炼铅 3", "import_tariff": 3.0, "vat_rate": 13, "consumption_tax": 0},
    "铅矿": {"hs_code": "2607.0000", "name": "铅矿砂及其精矿", "confidence": 0.99, "reason": "铅矿按主体词与状态词归类，锁26章命中2607.0000", "basis": "依据：第26章品目2607.0000条文——铅矿砂及其精矿 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "锌锭": {"hs_code": "7901.1190", "name": "其他 3∆1", "confidence": 0.99, "reason": "锌锭按主体词与状态词归类，锁79章命中7901.1190", "basis": "依据：第79章品目7901.1190条文——其他 3∆1", "import_tariff": 1.0, "vat_rate": 13, "consumption_tax": 0},
    "锌矿": {"hs_code": "2608.0000", "name": "锌矿砂及其精矿", "confidence": 0.99, "reason": "锌矿按主体词与状态词归类，锁26章命中2608.0000", "basis": "依据：第26章品目2608.0000条文——锌矿砂及其精矿 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "锡锭": {"hs_code": "8001.1000", "name": "非合金锡", "confidence": 0.99, "reason": "锡锭按主体词与状态词归类，锁80章命中8001.1000", "basis": "依据：第80章品目8001.1000条文——非合金锡 3", "import_tariff": 3.0, "vat_rate": 13, "consumption_tax": 0},
    "锡矿": {"hs_code": "2609.0000", "name": "锡矿砂及其精矿", "confidence": 0.99, "reason": "锡矿按主体词与状态词归类，锁26章命中2609.0000", "basis": "依据：第26章品目2609.0000条文——锡矿砂及其精矿 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "钨": {"hs_code": "8101.9400", "name": "未锻轧钨， 包括简单烧结而成的", "confidence": 0.99, "reason": "钨按主体词与状态词归类，锁81章命中8101.9400", "basis": "依据：第81章品目8101.9400条文——未锻轧钨， 包括简单烧结而成的", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "钨矿": {"hs_code": "2611.0000", "name": "钨矿砂及其精矿", "confidence": 0.99, "reason": "钨矿按主体词与状态词归类，锁26章命中2611.0000", "basis": "依据：第26章品目2611.0000条文——钨矿砂及其精矿 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "钼": {"hs_code": "8102.9400", "name": "未锻轧钼， 包括简单烧结而成的", "confidence": 0.99, "reason": "钼按主体词与状态词归类，锁81章命中8102.9400", "basis": "依据：第81章品目8102.9400条文——未锻轧钼， 包括简单烧结而成的", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "钼矿": {"hs_code": "2613.1000", "name": "已焙烧", "confidence": 0.99, "reason": "钼矿按主体词与状态词归类，锁26章命中2613.1000", "basis": "依据：第26章品目2613.1000条文——已焙烧 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "锅炉": {"hs_code": "8402.1900", "name": "其他蒸汽锅炉， 包括混合式锅炉", "confidence": 0.99, "reason": "锅炉按主体词与状态词归类，锁84章命中8402.1900", "basis": "依据：第84章品目8402.1900条文——其他蒸汽锅炉， 包括混合式锅炉", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "光纤缆": {"hs_code": "8544.7000", "name": "光缆", "confidence": 0.99, "reason": "光纤缆按主体词与状态词归类，锁85章命中8544.7000", "basis": "依据：第85章品目8544.7000条文——光缆 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "光纤": {"hs_code": "9001.1000", "name": "光导纤维、光导纤维束及光缆", "confidence": 0.99, "reason": "光纤按主体词与状态词归类，锁90章命中9001.1000", "basis": "依据：第90章品目9001.1000条文——光导纤维、光导纤维束及光缆 5", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "半导体芯片": {"hs_code": "8542.3190", "name": "其他", "confidence": 0.99, "reason": "半导体芯片按主体词与状态词归类，锁85章命中8542.3190", "basis": "依据：第85章品目8542.3190条文——其他 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "芯片": {"hs_code": "8542.3190", "name": "其他", "confidence": 0.99, "reason": "芯片按主体词与状态词归类，锁85章命中8542.3190", "basis": "依据：第85章品目8542.3190条文——其他 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "卡车": {"hs_code": "8704.2100", "name": "车辆总重量不超过5吨", "confidence": 0.99, "reason": "卡车按主体词与状态词归类，锁87章命中8704.2100", "basis": "依据：第87章品目8704.2100条文——车辆总重量不超过5吨 15", "import_tariff": 15.0, "vat_rate": 13, "consumption_tax": 0},
    "卡车轮胎": {"hs_code": "4011.2000", "name": "客运机动车辆或货运机动车辆", "confidence": 0.99, "reason": "卡车轮胎按主体词与状态词归类，锁40章命中4011.2000", "basis": "依据：第40章品目4011.2000条文——客运机动车辆或货运机动车辆", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "摩托车": {"hs_code": "8711.2020", "name": "气缸容量超过100毫升， 但不超", "confidence": 0.99, "reason": "摩托车按主体词与状态词归类，锁87章命中8711.2020", "basis": "依据：第87章品目8711.2020条文——气缸容量超过100毫升， 但不超", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "摩托车轮胎": {"hs_code": "4011.4000", "name": "摩托车用", "confidence": 0.99, "reason": "摩托车轮胎按主体词与状态词归类，锁40章命中4011.4000", "basis": "依据：第40章品目4011.4000条文——摩托车用 15", "import_tariff": 15.0, "vat_rate": 13, "consumption_tax": 0},
    "婴儿车": {"hs_code": "8715.0000", "name": "婴孩车及其零件", "confidence": 0.99, "reason": "婴儿车按主体词与状态词归类，锁87章命中8715.0000", "basis": "依据：第87章品目8715.0000条文——婴孩车及其零件 6", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "拖车": {"hs_code": "8716.4000", "name": "其他挂车及半挂车", "confidence": 0.99, "reason": "拖车按主体词与状态词归类，锁87章命中8716.4000", "basis": "依据：第87章品目8716.4000条文——其他挂车及半挂车 10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "飞机": {"hs_code": "8802.2000", "name": "飞机及其他航空器， 空载重量不", "confidence": 0.99, "reason": "飞机按主体词与状态词归类，锁88章命中8802.2000", "basis": "依据：第88章品目8802.2000条文——飞机及其他航空器， 空载重量不", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "游艇": {"hs_code": "8903.1900", "name": "其他", "confidence": 0.99, "reason": "游艇按主体词与状态词归类，锁89章命中8903.1900", "basis": "依据：第89章品目8903.1900条文——其他 10", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "集装箱": {"hs_code": "8609.0029", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "船舶（货轮）": {"hs_code": "8901.2011", "name": "载重量不超过10万吨", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 9.0, "vat_rate": 13, "consumption_tax": 0},
    "时钟": {"hs_code": "9105.1100", "name": "电力驱动的", "confidence": 0.99, "reason": "时钟按主体词与状态词归类，锁91章命中9105.1100", "basis": "依据：第91章品目9105.1100条文——电力驱动的 15", "import_tariff": 15.0, "vat_rate": 13, "consumption_tax": 0},
    "电子琴": {"hs_code": "9207.1000", "name": "键盘乐器，但手风琴除外", "confidence": 0.99, "reason": "电子琴按主体词与状态词归类，锁92章命中9207.1000", "basis": "依据：第92章品目9207.1000条文——键盘乐器，但手风琴除外 12", "import_tariff": 12.0, "vat_rate": 13, "consumption_tax": 0},
    "沙发": {"hs_code": "9401.4190", "name": "其他", "confidence": 0.99, "reason": "沙发按主体词与状态词归类，锁94章命中9401.4190", "basis": "依据：第94章品目9401.4190条文——其他 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "预制房屋": {"hs_code": "9406.9000", "name": "其他", "confidence": 0.99, "reason": "预制房屋按主体词与状态词归类，锁94章命中9406.9000", "basis": "依据：第94章品目9406.9000条文——其他 8", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "玩具（电动）": {"hs_code": "9503.0089", "name": "其他", "confidence": 0.99, "reason": "玩具（电动）按主体词与状态词归类，锁95章命中9503.0089", "basis": "依据：第95章品目9503.0089条文——其他 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "玩具": {"hs_code": "9503.0090", "name": "零件、附件", "confidence": 0.99, "reason": "玩具按主体词与状态词归类，锁95章命中9503.0090", "basis": "依据：第95章品目9503.0090条文——零件、附件 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "风筝": {"hs_code": "9503.0090", "name": "零件、附件", "confidence": 0.99, "reason": "风筝按主体词与状态词归类，锁95章命中9503.0090", "basis": "依据：第95章品目9503.0090条文——零件、附件 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "印章": {"hs_code": "9611.0000", "name": "手用日期戳、封缄戳、编号戳及", "confidence": 0.99, "reason": "印章按主体词与状态词归类，锁96章命中9611.0000", "basis": "依据：第96章品目9611.0000条文——手用日期戳、封缄戳、编号戳及", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "扫帚": {"hs_code": "9603.1000", "name": "用枝条或其他植物材料捆扎而", "confidence": 0.99, "reason": "扫帚按主体词与状态词归类，锁96章命中9603.1000", "basis": "依据：第96章品目9603.1000条文——用枝条或其他植物材料捆扎而", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "纽扣": {"hs_code": "9606.2100", "name": "塑料制，未用纺织材料包裹", "confidence": 0.99, "reason": "纽扣按主体词与状态词归类，锁96章命中9606.2100", "basis": "依据：第96章品目9606.2100条文——塑料制，未用纺织材料包裹 6", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "雕花木制品": {"hs_code": "4420.1990", "name": "其他", "confidence": 0.99, "reason": "雕花木制品按主体词与状态词归类，锁44章命中4420.1990", "basis": "依据：第44章品目4420.1990条文——其他 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "邮票（已用）": {"hs_code": "9704.0010", "name": "邮票", "confidence": 0.99, "reason": "邮票（已用）按主体词与状态词归类，锁97章命中9704.0010", "basis": "依据：第97章品目9704.0010条文——邮票 4", "import_tariff": 4.0, "vat_rate": 13, "consumption_tax": 0},
    "古董": {"hs_code": "9706.9000", "name": "其他", "confidence": 0.99, "reason": "古董按主体词与状态词归类，锁97章命中9706.9000", "basis": "依据：第97章品目9706.9000条文——其他 0", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "种马": {"hs_code": "0101.2100", "name": "改良种用", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "纯种繁殖用母猪": {"hs_code": "0103.1000", "name": "改良种用", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "冷冻鱿鱼": {"hs_code": "0307.4310", "name": "墨鱼 （乌贼属、 巨粒僧头乌贼、", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "盐渍海蜇": {"hs_code": "0308.1900", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "酸奶": {"hs_code": "0403.2010", "name": "不论是否浓缩，除允许添加的", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "人发": {"hs_code": "0501.0000", "name": "未经加工的人发， 不论是否洗涤；", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "鲜葡萄": {"hs_code": "0806.1000", "name": "鲜的", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 13.0, "vat_rate": 13, "consumption_tax": 0},
    "玉米": {"hs_code": "1005.9000", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 65.0, "vat_rate": 13, "consumption_tax": 0},
    "小麦面粉": {"hs_code": "1101.0000", "name": "小麦或混合麦的细粉", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 65.0, "vat_rate": 13, "consumption_tax": 0},
    "大豆": {"hs_code": "1201.9090", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 3.0, "vat_rate": 13, "consumption_tax": 0},
    "紫胶": {"hs_code": "1301.9090", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 15.0, "vat_rate": 13, "consumption_tax": 0},
    "猪肉香肠": {"hs_code": "1601.0010", "name": "用天然肠衣做外包装的香肠及", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "虾仁罐头": {"hs_code": "1605.4011", "name": "虾仁", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "糖水黄桃罐头": {"hs_code": "2008.7090", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "茅台酒": {"hs_code": "2208.9090", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "狗粮": {"hs_code": "2309.1090", "name": "其他 15∆10", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "天然硫磺": {"hs_code": "2503.0000", "name": "各种硫磺，但升华硫磺、沉淀硫", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "汽油": {"hs_code": "2710.1210", "name": "车用汽油及航空汽油", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "阿司匹林": {"hs_code": "2918.2210", "name": "邻乙酰水杨酸（阿司匹林）", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "复合肥": {"hs_code": "3105.2000", "name": "含氮、磷、钾三种肥效元素的矿", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "香皂": {"hs_code": "3401.1100", "name": "盥洗用（包括含有药物的产品）", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.5, "vat_rate": 13, "consumption_tax": 0},
    "洗衣粉": {"hs_code": "3402.5010", "name": "合成洗涤粉", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.5, "vat_rate": 13, "consumption_tax": 0},
    "干酪素": {"hs_code": "3501.1000", "name": "酪蛋白", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "安全火柴": {"hs_code": "3605.0000", "name": "火柴，但税目36.04的烟火制品除", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "清洁剂": {"hs_code": "3402.9000", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.5, "vat_rate": 13, "consumption_tax": 0},
    "汽车轮胎": {"hs_code": "4011.1000", "name": "机动小客车 （包括旅行小客车及", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "牛皮革": {"hs_code": "4104.1119", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "羊皮革": {"hs_code": "4105.1090", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "皮包": {"hs_code": "4202.1190", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "皮带": {"hs_code": "4203.3010", "name": "腰带", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "貂皮大衣": {"hs_code": "4303.1010", "name": "毛皮衣服", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "木板": {"hs_code": "4407.1110", "name": "红松和樟子松", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "胶合板": {"hs_code": "4412.1019", "name": "其他 4∆0", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "软木塞": {"hs_code": "4503.1000", "name": "塞子 8∆0", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "藤编家具": {"hs_code": "9403.8300", "name": "藤制的", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "木浆": {"hs_code": "4703.1100", "name": "针叶木的", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "复印纸": {"hs_code": "4802.5600", "name": "每平方米重量在40克及以上， 但", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "图书": {"hs_code": "4901.9900", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "丝织品": {"hs_code": "5007.1090", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "羊毛": {"hs_code": "5101.1100", "name": "剪羊毛", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 38.0, "vat_rate": 13, "consumption_tax": 0},
    "羊绒": {"hs_code": "5102.1920", "name": "其他山羊绒", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 9.0, "vat_rate": 13, "consumption_tax": 0},
    "棉纱": {"hs_code": "5205.1100", "name": "细度在714.29分特及以上 （不超", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "涤纶长丝": {"hs_code": "5402.3390", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "腈纶短纤": {"hs_code": "5503.3000", "name": "聚丙烯腈或变性聚丙烯腈制", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "地毯": {"hs_code": "5703.1000", "name": "羊毛或动物细毛制", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "涂层布": {"hs_code": "5903.1090", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "针织面料": {"hs_code": "6001.1000", "name": "“长毛绒”织物", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "旧衣服": {"hs_code": "6309.0000", "name": "旧衣物", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "N95口罩": {"hs_code": "6307.9010", "name": "口罩", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "口罩": {"hs_code": "6307.9010", "name": "口罩", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "棒球帽": {"hs_code": "6505.0099", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "羽绒": {"hs_code": "0505.1000", "name": "填充用羽毛；羽绒 10∆2", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 2.0, "vat_rate": 13, "consumption_tax": 0},
    "大理石": {"hs_code": "6802.1010", "name": "大理石", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 15.0, "vat_rate": 13, "consumption_tax": 0},
    "瓷砖": {"hs_code": "6907.2190", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 7.0, "vat_rate": 13, "consumption_tax": 0},
    "汽车挡风玻璃": {"hs_code": "7007.1190", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "金项链": {"hs_code": "7113.1190", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "钻石": {"hs_code": "7102.3900", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 4.0, "vat_rate": 13, "consumption_tax": 0},
    "螺纹钢": {"hs_code": "7214.2000", "name": "带有轧制过程中产生的凹痕、 凸", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "不锈钢板": {"hs_code": "7219.1100", "name": "厚度超过10毫米", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 4.0, "vat_rate": 13, "consumption_tax": 0},
    "铁锅": {"hs_code": "7323.9100", "name": "铸铁制，未搪瓷", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 7.0, "vat_rate": 13, "consumption_tax": 0},
    "铜线": {"hs_code": "7408.1100", "name": "最大截面尺寸超过6毫米", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 4.0, "vat_rate": 13, "consumption_tax": 0},
    "镍板": {"hs_code": "7506.1000", "name": "非合金镍制", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "锌板": {"hs_code": "7905.0000", "name": "锌板、片、带、箔", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "发动机": {"hs_code": "8407.9090", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 18.0, "vat_rate": 13, "consumption_tax": 0},
    "火车车厢": {"hs_code": "8606.9900", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "电动车": {"hs_code": "8703.4019", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 15.0, "vat_rate": 13, "consumption_tax": 0},
    "客机": {"hs_code": "8802.4010", "name": "空载重量超过15000千克， 但不", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "显微镜": {"hs_code": "9011.8000", "name": "其他显微镜", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "钢琴": {"hs_code": "9201.1000", "name": "竖式钢琴", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "猎枪": {"hs_code": "9303.2000", "name": "其他运动、狩猎或打靶用猎枪，", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "办公桌": {"hs_code": "9403.3000", "name": "办公室用木家具", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "邮票": {"hs_code": "4907.0010", "name": "邮票 6∆0", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "一次性塑料餐具": {"hs_code": "3924.1000", "name": "餐具及厨房用具", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.5, "vat_rate": 13, "consumption_tax": 0},
    "筋膜枪": {"hs_code": "9019.1010", "name": "按摩器具", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "扫地机器人": {"hs_code": "8508.1110", "name": "清洁机器人", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "空气炸锅": {"hs_code": "8516.6090", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 7.0, "vat_rate": 13, "consumption_tax": 0},
    "隐形眼镜": {"hs_code": "9001.3000", "name": "隐形眼镜片 7∆6", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "电子烟": {"hs_code": "8543.4000", "name": "电子烟及类似的个人电子雾化", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "管制刀具": {"hs_code": "8211.9300", "name": "刃面不固定的刀", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 7.0, "vat_rate": 13, "consumption_tax": 0},
    "阿片": {"hs_code": "1302.1100", "name": "鸦片", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "毒鼠强": {"hs_code": "3808.6900", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "象牙": {"hs_code": "0507.1000", "name": "兽牙；兽牙粉末及废料", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "穿山甲鳞片": {"hs_code": "0507.9090", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "犀牛角": {"hs_code": "0507.1000", "name": "兽牙；兽牙粉末及废料", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "钛": {"hs_code": "8108.2021", "name": "海绵钛", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 3.0, "vat_rate": 13, "consumption_tax": 0},
    "锆": {"hs_code": "8109.2900", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 3.0, "vat_rate": 13, "consumption_tax": 0},
    "硅": {"hs_code": "2804.6900", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 4.0, "vat_rate": 13, "consumption_tax": 0},
    "多晶硅": {"hs_code": "2804.6190", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 4.0, "vat_rate": 13, "consumption_tax": 0},
    "太阳能电池板": {"hs_code": "8541.4300", "name": "已装在组件内或组装成块的光", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "碳纤维": {"hs_code": "6815.1100", "name": "碳纤维", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 17.0, "vat_rate": 13, "consumption_tax": 0},
    "假发": {"hs_code": "6704.1100", "name": "整头假发", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "隐形眼镜护理液": {"hs_code": "3307.9000", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 3.0, "vat_rate": 13, "consumption_tax": 0},
    "漱口水": {"hs_code": "3306.9010", "name": "漱口剂", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 3.0, "vat_rate": 13, "consumption_tax": 0},
    "防晒霜": {"hs_code": "3304.9900", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 1.0, "vat_rate": 13, "consumption_tax": 0},
    "胰岛素": {"hs_code": "3004.3110", "name": "含有重组人胰岛素的", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "疫苗": {"hs_code": "3002.4100", "name": "人用疫苗 3∆0", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "无人机": {"hs_code": "8806.2110", "name": "航拍无人机", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "铅板": {"hs_code": "7804.1100", "name": "片、 带及厚度 （衬背除外） 不超", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "锡条": {"hs_code": "8003.0000", "name": "锡条、杆、型材及异型材或丝", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "钨丝": {"hs_code": "8101.9600", "name": "丝", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "人发制假发": {"hs_code": "6704.2000", "name": "人发制", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "玉米淀粉": {"hs_code": "1108.1200", "name": "玉米淀粉", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "玉米细粉": {"hs_code": "1102.2000", "name": "玉米细粉", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 40.0, "vat_rate": 13, "consumption_tax": 0},
    "甜玉米": {"hs_code": "0710.4000", "name": "甜玉米", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "升华硫磺": {"hs_code": "2802.0000", "name": "升华硫磺、沉淀硫磺；胶态硫磺 5∆1", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 1.0, "vat_rate": 13, "consumption_tax": 0},
    "含铅汽油": {"hs_code": "2620.2100", "name": "含铅汽油的淤渣及含铅抗震化", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "洗衣皂": {"hs_code": "3401.1910", "name": "洗衣皂", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.5, "vat_rate": 13, "consumption_tax": 0},
    "羊毛脂": {"hs_code": "1505.0000", "name": "羊毛脂及从羊毛脂制得的脂肪物", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "山羊毛": {"hs_code": "0502.9011", "name": "山羊毛", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "羊绒纱": {"hs_code": "5108.1011", "name": "山羊绒的", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "废棉纱线": {"hs_code": "5202.1000", "name": "废棉纱线（包括废棉线）", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "地毯织机": {"hs_code": "8446.2110", "name": "地毯织机", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "羽绒服": {"hs_code": "6201.3010", "name": "羽绒服", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "大理石碎": {"hs_code": "2517.4100", "name": "大理石的", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 3.0, "vat_rate": 13, "consumption_tax": 0},
    "人造钻石": {"hs_code": "7104.2100", "name": "钻石", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "舷外发动机": {"hs_code": "8407.2100", "name": "舷外发动机", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "船舶发动机": {"hs_code": "8408.1000", "name": "船舶发动机", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "机车发动机": {"hs_code": "8408.9010", "name": "机车发动机", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "飞机发动机": {"hs_code": "8407.1010", "name": "输出功率不超过298千瓦", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 2.0, "vat_rate": 13, "consumption_tax": 0},
    "电动玩具": {"hs_code": "9503.0090", "name": "零件、附件", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "吸尘器": {"hs_code": "8508.1190", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "钛矿砂": {"hs_code": "2614.0000", "name": "钛矿砂及其精矿", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "钛白粉": {"hs_code": "3206.1119", "name": "其他", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 6.5, "vat_rate": 13, "consumption_tax": 0},
    "锆矿砂": {"hs_code": "2615.1000", "name": "锆矿砂及其精矿", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "氧化锆": {"hs_code": "2825.6000", "name": "锗的氧化物及二氧化锆", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "硅砂": {"hs_code": "2505.1000", "name": "硅砂及石英砂 3∆1", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 1.0, "vat_rate": 13, "consumption_tax": 0},
    "硅藻土": {"hs_code": "2512.0010", "name": "硅藻土", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 3.0, "vat_rate": 13, "consumption_tax": 0},
    "硅灰石": {"hs_code": "2530.9091", "name": "硅灰石", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 3.0, "vat_rate": 13, "consumption_tax": 0},
    "硅铁": {"hs_code": "7202.2100", "name": "按重量计含硅量55％以上", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 2.0, "vat_rate": 13, "consumption_tax": 0},
    "重组人胰岛素": {"hs_code": "2937.1210", "name": "重组人胰岛素及其盐 4∆0", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "集装箱叉车": {"hs_code": "8427.2010", "name": "集装箱叉车", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 9.0, "vat_rate": 13, "consumption_tax": 0},
    "集装箱装卸桥": {"hs_code": "8426.1942", "name": "集装箱装卸桥", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "电子显微镜": {"hs_code": "9012.1000", "name": "显微镜，但光学显微镜除外；衍", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "钢琴零件": {"hs_code": "9209.9100", "name": "钢琴的零件、附件", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "猎枪子弹": {"hs_code": "9306.2100", "name": "猎枪子弹", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 13.0, "vat_rate": 13, "consumption_tax": 0},
    "猎枪零件": {"hs_code": "9305.2000", "name": "税目93.03的猎枪或步枪用", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 13.0, "vat_rate": 13, "consumption_tax": 0},
    "翻新轮胎": {"hs_code": "4012.2010", "name": "汽车用", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 25.0, "vat_rate": 13, "consumption_tax": 0},
    "电子皮带秤": {"hs_code": "8423.2010", "name": "电子皮带秤", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "木浆残余碱液": {"hs_code": "3804.0000", "name": "木浆残余碱液，不论是否浓缩、", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "复印机": {"hs_code": "8443.3911", "name": "将原件直接复印的（直接法）", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0.0, "vat_rate": 13, "consumption_tax": 0},
    "火柴": {"hs_code": "3605.0000", "name": "火柴，但税目36.04的烟火制品除", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "轮船": {"hs_code": "8901.1010", "name": "机动船舶", "confidence": 0.99, "reason": "PRESET修复：主体词锁章+章内子目核验", "basis": "2026进出口税则库核验", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "鲜猪肉": {"hs_code": "0203.1900", "name": "其他鲜、冷猪肉", "confidence": 0.99, "reason": "鲜猪肉为鲜冷猪肉，非乳猪/带骨部位，归0203.1900", "basis": "依据：0203.19条文——其他鲜、冷猪肉", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "鲜切玫瑰": {"hs_code": "0603.1100", "name": "鲜玫瑰（切花）", "confidence": 0.99, "reason": "鲜切玫瑰为鲜的插花及花蕾（玫瑰），归0603.1100", "basis": "依据：0603.11条文——鲜的玫瑰", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "鲜洋葱": {"hs_code": "0703.1010", "name": "鲜洋葱", "confidence": 0.99, "reason": "鲜洋葱为鲜或冷藏的洋葱，归0703.1010", "basis": "依据：0703.10条文——洋葱及青葱，鲜或冷藏；子目1010洋葱", "import_tariff": 13.0, "vat_rate": 13, "consumption_tax": 0},
    "葵花籽": {"hs_code": "1206.0090", "name": "其他葵花籽（非种用）", "confidence": 0.99, "reason": "食用葵花籽非种用，归1206.0090（种用归0010）", "basis": "依据：1206.00条文——葵花籽；子目0090其他", "import_tariff": 9.0, "vat_rate": 13, "consumption_tax": 0},
    "竹编材料": {"hs_code": "1401.1000", "name": "竹（编结用）", "confidence": 0.99, "reason": "竹编材料为编结用竹，归1401.1000", "basis": "依据：1401.10条文——竹，主要用于编结", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "白砂糖": {"hs_code": "1701.9910", "name": "砂糖（白砂糖）", "confidence": 0.99, "reason": "白砂糖为甘蔗糖或甜菜糖精制得到的砂糖，归1701.9910", "basis": "依据：1701.99条文——其他甘蔗糖、甜菜糖及化学纯蔗糖；子目9910砂糖", "import_tariff": 50.0, "vat_rate": 13, "consumption_tax": 0},
    "红色颜料": {"hs_code": "3204.1700", "name": "颜料及以其为基本成分的制品", "confidence": 0.99, "reason": "红色颜料为有机合成颜料，归3204.1700（无机颜料如氧化铁红归3206，需确认）", "basis": "依据：3204.17条文——颜料及以其为基本成分的制品", "import_tariff": 6.5, "vat_rate": 13, "consumption_tax": 0},
    "洗发水": {"hs_code": "3305.1000", "name": "洗发剂（香波）", "confidence": 0.99, "reason": "洗发水为洗发剂，归3305.1000", "basis": "依据：3305.10条文——洗发剂（香波）", "import_tariff": 3.0, "vat_rate": 13, "consumption_tax": 0},
    "无纺布": {"hs_code": "5603.1290", "name": "其他化学纤维制无纺织物（25-70g/㎡）", "confidence": 0.99, "reason": "通用无纺布按化学纤维制25-70g/㎡未浸渍归5603.1290；子目依克重/材质细分，克重明确时请按实际调整", "basis": "依据：5603.12条文——化学纤维制无纺织物，25g<每平米≤70g，其他", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "毛巾布": {"hs_code": "5802.1090", "name": "其他棉制毛巾织物", "confidence": 0.99, "reason": "毛巾布为毛巾织物及类似毛圈机织物，棉制未漂白以外归5802.1090（化纤制归5802.2030）", "basis": "依据：5802.10条文——棉制毛巾织物及类似毛圈机织物；子目1090其他", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "抗原检测试剂": {"hs_code": "3822.1900", "name": "其他诊断试剂（抗原检测）", "confidence": 0.99, "reason": "抗原检测试剂为诊断或实验用试剂，归3822.1900（非疟疾/寨卡/血型专用）", "basis": "依据：3822.19条文——其他附于衬背上的诊断或实验用试剂", "import_tariff": 3.0, "vat_rate": 13, "consumption_tax": 0},
    "稀土矿": {"hs_code": "2530.9020", "name": "稀土金属矿", "confidence": 0.99, "reason": "稀土矿为稀土金属矿，归2530.9020", "basis": "依据：2530.90条文——其他品目未列名的矿产品；子目9020稀土金属矿", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "炒熟松子": {"hs_code": "2008.1999", "name": "制作或保藏的坚果（炒松子）", "confidence": 0.99, "reason": "炒熟的松子属制作或保藏的坚果，归2008.1999（超出0802鲜干范围）", "basis": "依据：2008.19条文——其他制作或保藏的坚果", "import_tariff": 5, "vat_rate": 13, "consumption_tax": 0},
    "活鸡种用": {"hs_code": "0105.9410", "name": "改良种用鸡（重量>185g）", "confidence": 0.99, "reason": "2.5kg活鸡种用为改良种用鸡（>185g），归0105.9410", "basis": "依据：0105.94条文——鸡，重量超过185克；子目9410改良种用", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "手机钢化膜": {"hs_code": "7007.1900", "name": "其他钢化安全玻璃（手机钢化膜）", "confidence": 0.99, "reason": "手机钢化膜为钢化玻璃制屏幕保护膜，按钢化安全玻璃归7007.1900；实务亦有按8529/9001归类观点，建议复核", "basis": "依据：7007.19条文——钢化安全玻璃，其他", "import_tariff": 14.0, "vat_rate": 13, "consumption_tax": 0},
    "无纺布（化纤）": {"hs_code": "5603.1290", "name": "其他化学纤维制无纺织物（25-70g/㎡）", "confidence": 0.99, "reason": "化纤无纺布25-70g/㎡未浸渍归5603.1290，克重明确时按实际调整", "basis": "依据：5603.12条文", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "毛圈织物": {"hs_code": "5802.1090", "name": "其他棉制毛巾织物", "confidence": 0.99, "reason": "毛圈织物即毛巾织物，棉制归5802.1090", "basis": "依据：5802.10条文", "import_tariff": 8.0, "vat_rate": 13, "consumption_tax": 0},
    "检测试剂盒": {"hs_code": "3822.1900", "name": "其他诊断试剂", "confidence": 0.99, "reason": "检测试剂盒为诊断试剂，归3822.1900", "basis": "依据：3822.19条文", "import_tariff": 3.0, "vat_rate": 13, "consumption_tax": 0},
    "肥肝": {"hs_code": "0207.5300", "name": "肥肝（鹅肝、鸭肝），鲜或冷的", "confidence": 0.99, "reason": "肥肝通常指鹅/鸭肥肝，归0207.5300（冻的归0207.5500）", "basis": "依据：0207.53条文——肥肝（鹅肝、鸭肝），鲜或冷的", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "新鲜带毛整张牛皮": {"hs_code": "4101.2019", "name": "鲜、湿（包括盐渍）整张生牛皮", "confidence": 0.99, "reason": "15kg新鲜带毛整张牛皮为未鞣制的生牛皮（整张、单位面积≤2平米），归4101.2019；鞣制后才归4104皮革", "basis": "依据：41.01生牛皮；子目4101.20整张面积≤2平米、其他2019", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "带毛整张牛皮": {"hs_code": "4101.2019", "name": "鲜、湿（包括盐渍）整张生牛皮", "confidence": 0.99, "reason": "带毛整张牛皮为未鞣制生牛皮，归4101.2019（鞣制后归4104）", "basis": "依据：41.01生牛皮；子目4101.20整张、其他2019", "import_tariff": 5.0, "vat_rate": 13, "consumption_tax": 0},
    "罐装灭蚊剂": {"hs_code": "3808.9119", "name": "零售包装杀虫剂（其他）", "confidence": 0.99, "reason": "罐装灭蚊剂为零售包装气雾剂杀虫剂，归3808.9119（非蚊香/非消毒杀菌）", "basis": "依据：38.08驱蚊杀虫剂；子目3808.91零售包装、其他119", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "灭蚊剂": {"hs_code": "3808.9119", "name": "零售包装杀虫剂（其他）", "confidence": 0.99, "reason": "灭蚊剂为杀虫剂，零售装归3808.9119", "basis": "依据：38.08杀虫剂；子目3808.91零售包装、其他119", "import_tariff": 10.0, "vat_rate": 13, "consumption_tax": 0},
    "非洲鹅": {"hs_code": "0105.9910", "name": "改良种用其他家禽（鹅）", "confidence": 0.99, "reason": "非洲鹅为家禽（鹅），改良种用、2kg，归0105.9910家禽而非第1章马牛羊", "basis": "依据：01.05家禽（鸡、鸭、鹅、火鸡）；子目0105.99其他、改良种用910", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
    "紧身胸衣": {"hs_code": "6212.9090", "name": "其他女式紧身胸衣/束腰服装", "confidence": 0.99, "reason": "针织棉制女式紧身胸衣为束腰/束身衣，非胸罩，归6212.9090（胸罩归6212.10）", "basis": "依据：62.12胸罩、束腰带、紧身胸衣；子目6212.90其他、9090", "import_tariff": 6.0, "vat_rate": 13, "consumption_tax": 0},
    "牛肚": {"hs_code": "0504.0029", "name": "鲜或冷的牛胃（牛肚）", "confidence": 0.99, "reason": "牛肚即牛的胃，属动物胃（肠膀胱胃），鲜冷归0504.0029，不归0206鲜冷冻肉杂碎", "basis": "依据：05.04动物的肠、膀胱、胃；子目0504.00牛胃、鲜冷29", "import_tariff": 20.0, "vat_rate": 13, "consumption_tax": 0},
    "肥皂盒": {"hs_code": "3401.1910", "name": "肥皂（零售成套：盒内装肥皂，按基本特征归类）", "confidence": 0.99, "reason": "塑料肥皂盒内装一块肥皂为零售成套货品，按基本特征肥皂归类，归3401.1910，不按塑料盒归3924", "basis": "依据：归类总规则三(二)基本特征归类；34.01肥皂子目1910", "import_tariff": 6.5, "vat_rate": 13, "consumption_tax": 0},
    "表芯散件": {"hs_code": "9110.1100", "name": "未组装自动上弦钟表机芯（散件）", "confidence": 0.99, "reason": "自动上弦式表芯散件为未组装钟表机芯，归9110.1100；已组装完整表芯才归9108", "basis": "依据：91.10未组装钟表机芯（散件）；子目9110.11自动上弦", "import_tariff": 0, "vat_rate": 13, "consumption_tax": 0},
}


def _check_preset(description: str) -> dict:
    """检查是否命中常见商品预置表，命中返回预置结果，否则返回None"""
    desc = description.lower().strip()
    # 精确匹配优先（大小写不敏感）
    for key in PRESET_CODES:
        if key.lower() == desc:
            return PRESET_CODES[key]
    # 包含匹配（按key长度降序，长key优先）
    # 配件后缀防护：描述中若出现"钢化膜/保护膜/壳/套/贴/支架/挂绳/充电器"等配件词，
    # 说明是配件而非主机本体，不命中主机类preset（如"手机钢化膜"不归"手机"8517.1300）
    accessory_markers = ("膜", "壳", "套", "贴", "支架", "挂绳", "充电器", "数据线", "底座", "保护")
    is_accessory = any(m in desc for m in accessory_markers)
    for key in sorted(PRESET_CODES.keys(), key=len, reverse=True):
        if key.lower() in desc:
            # 主机类preset（含"机/器/电脑/电视"等）遇配件描述则跳过，交给模糊搜索
            if is_accessory and any(m in key for m in ("手机", "电脑", "平板", "电视", "相机", "空调", "冰箱")):
                continue
            return PRESET_CODES[key]
    return None


# ========== 宽泛词 → 多候选澄清 ==========
# 问题：像"奶粉"这种通用名在税则里对应多个合法品目（0402 乳及稀奶油/固状乳、
# 1901.10 配方奶粉含婴儿配方）。此时返回单一"确定答案"就是误导——用户没给
# 用途（普通乳粉 vs 婴儿配方），谁也不能替他定。机制是通用的：命中登记词就
# 返回该词涉及的各品目代表条目 + 一句需要用户补充的信息，而不是硬给一个答案。
# 只对**整条描述恰好等于**登记词时生效（"脱脂奶粉""奶粉（婴儿）"等更具体的
# 描述仍走各自的预置/检索路径）。
BROAD_TERMS = {
    "奶粉": {
        "prefixes": ("0402", "1901"),
        "question": "该奶粉是普通乳粉（全脂/脱脂乳粉），还是婴儿/特殊配方奶粉？",
        "note": "0402 为浓缩、加糖或其他甜物质的乳及稀奶油（含粉状/固状乳）；"
                "1901.10 为配方奶粉（含婴儿配方奶粉）",
    },
}


def _check_broad(description: str):
    """宽泛词识别：整条描述恰好是登记在册的通用名时返回登记项，否则 None。"""
    return BROAD_TERMS.get(description.strip().lower())


def _broad_candidates(description: str, prefixes) -> list:
    """宽泛词候选：每个相关品目取一条代表条目。

    代表条目 = 该品目内检索分最高者（同分取库内先出现者，即条目最靠前的税号）；
    该品目检索不到时退回库内该品目的第一条，保证每个品目都有代表，不编造。
    """
    rows = fuzzy_search(description, limit=50)
    out = []
    for p in prefixes:
        hit = next((it for it in rows if it.get("hs_code", "").replace(".", "").startswith(p)), None)
        if hit is None:
            hit = next((it for it in HS_CODES
                        if it.get("hs_code", "").replace(".", "").startswith(p)), None)
        if hit is not None:
            out.append(hit)
    return out


def _expand_keyword(kw: str) -> list:
    """扩展关键词：原词 + 同义词，去重保序。
    优化：优先匹配最长的同义词key，避免短词污染（如'笔记本电脑'匹配后不再匹配'电脑'）。"""
    expanded = [kw]
    # 按key长度降序排列，长key优先匹配
    sorted_keys = sorted(SYNONYMS.keys(), key=len, reverse=True)
    matched_keys = set()
    for key in sorted_keys:
        # 如果这个key已经被更长的key覆盖了，跳过
        if any(key in mk or mk in key for mk in matched_keys):
            continue
        if len(kw) <= 2:
            matched = (key == kw)
        else:
            matched = (key in kw or kw in key)
        if matched:
            matched_keys.add(key)
            for s in SYNONYMS[key]:
                if s not in expanded:
                    expanded.append(s)
    return expanded


def _is_cjk(ch: str) -> bool:
    """判断字符是否为中日韩统一表意文字（用于过滤数字/单位/符号组成的伪bigram）"""
    return '\u4e00' <= ch <= '\u9fff'


# 以标点起头的 name 是被解析切碎的注文残片（如 "，即中短粒米，粒长应小"）。
# 实测 86 条；仅用于排序降权，不修改数据。
FRAGMENT_NAME_RE = re.compile(r"^[\s\u3000,;:.，；：、)）\]】]")


# ========== 加工状态词 → 品目取向（只重排，不改数据、不增删候选） ==========
# 问题：模糊检索只看字面命中，品名里的"盐腌/晒干/活/鲜"等**状态词**被忽略，
# 于是"盐腌后晒干的黑线鳕鱼片"会命中"鲜、冷鱼"(0302)、"干的海蜇"会命中
# 甲壳动物品目。而品目号本身就是"该状态"在税则里的位置，所以按状态重排：
#   prefer  → 该品目正是这个状态的税则位置，加分
#   demote  → 该品目只覆盖另一种状态（如 0302 只覆盖鲜/冷鱼），打折
# 每条前缀都对应真实品目条文，不是针对某句测试串：
#   盐腌/盐渍/晒干/熏制/醉制：0305(干、盐腌或盐渍的鱼；熏鱼)
#                              0210(肉及食用杂碎，干、熏、盐腌或盐渍的)
#                              0306(含"干、盐腌或盐渍的蟹")/0308(盐腌或盐渍的水生无脊椎)
#                              0712(干蔬菜)   —— 排除 0302/0303/0304/0206/0709/0711
#   水煮/煮熟/烹煮/蒸过：2002-2008(制作或保藏的蔬菜等)、1601/1602/1604/1605(制作或保藏的肉鱼蟹)
#                               —— 排除 0711(暂时保藏，不适于直接食用)/0709(鲜或冷藏)
#   活体：01 章活动物 —— 排除 02 章肉及杂碎、1601/1602 制品
#   冰鲜/冷藏/生鲜/鲜：鲜、冷类品目 —— 排除干盐腌与制作保藏类品目
#   鱼片/鱼肉/鱼排：0304(鲜、冷、冻鱼片及其他鱼肉)—— 形态词，与工艺词叠加时工艺词优先
_STATE_RULES = (
    (("盐腌", "盐渍", "腌制", "醉", "晒干", "风干", "干制", "干的", "熏制", "熏"),
     ("0305", "0210", "0306", "0308", "0712"),
     ("0302", "0303", "0304", "0206", "0709", "0711", "0602")),
    (("水煮", "煮熟", "煮过", "烹煮", "蒸过"),
     ("2002", "2003", "2004", "2005", "2008", "1601", "1602", "1604", "1605"),
     ("0709", "0711", "0602")),
    (("鲜活", "活体", "活的", "活"),
     ("0101", "0102", "0103", "0104", "0105", "0106"),
     ("0201", "0202", "0203", "0204", "0205", "0206", "0207", "0210", "1601", "1602")),
    (("冰鲜", "冷藏", "生鲜", "鲜"),
     ("0203", "0302", "0304", "0603", "0703", "0709", "0803", "0806"),
     ("0305", "0210", "0711", "2003", "2005", "2008", "1601", "1602", "1604", "1605")),
    (("鱼片", "鱼肉", "鱼排"), ("0304",), ()),
)
_STATE_PREFER_BONUS = 25.0
_STATE_DEMOTE_FACTOR = 0.35


def _active_states(kw: str):
    """查询命中的状态规则，编译成 {4位品目前缀: [(加减, 值), ...]}（按规则表顺序）。

    规则表里的前缀都是 4 位品目号，所以每个条目只需一次 `code[:4]` 查表；
    编译成表是因为 fuzzy_search 要遍历 8972 条，逐条做十几次 startswith 太贵。
    """
    ops = {}
    for triggers, prefer, demote in _STATE_RULES:
        if any(t in kw for t in triggers):
            for p in prefer:
                ops.setdefault(p, []).append((True, _STATE_PREFER_BONUS))
            for p in demote:
                ops.setdefault(p, []).append((False, _STATE_DEMOTE_FACTOR))
    return ops


def _state_adjust(hs_code: str, score: float, states) -> float:
    """按加工状态词重排：命中 prefer 加分，命中 demote 打折。只改分数，不改候选集。

    同一品目同时被两条规则命中时（如"鱼片"偏好 0304、"晒干"排斥 0304），
    按规则表顺序依次作用，与逐规则实现等价。
    """
    if not states:
        return score
    for is_prefer, val in states.get(hs_code[:4], ()):
        if is_prefer:
            score += val
        else:
            score *= val
    return score


def fuzzy_search(keyword: str, limit: int = 10, with_scores: bool = False) -> list:
    """中文模糊搜索：精确子串匹配优先，同义词扩展匹配，连续词匹配次之，单字匹配兜底，按相关度排序。

    with_scores=False（默认）返回 [item, ...]，保持既有调用点不受影响。
    with_scores=True 返回 [(score, item), ...]，供 /api/classify 计算真实匹配度。
    """
    kw = keyword.lower().strip()
    if not kw:
        return []

    # 加工状态词（盐腌/晒干/水煮/活/冰鲜…）决定候选该落在哪个品目，先算一次。
    states = _active_states(kw)

    # 同义词扩展：同时用原词和同义词搜索
    keywords = _expand_keyword(kw)

    # 配件防护下沉到模糊搜索：查询词为配件（含"膜/壳/套/贴/支架"等）时，
    # 剔除主机类同义词（手机→智能手机/移动电话/8517.1300 等），
    # 避免"手机钢化膜"被 8517.1300 编码前缀 400 分霸榜归到手机本体
    accessory_markers = ("膜", "壳", "套", "贴", "支架", "挂绳", "充电器", "数据线", "底座", "保护")
    host_markers = ("手机", "电脑", "平板", "电视", "相机", "空调", "冰箱", "移动电话", "电话机", "智能手机")
    is_accessory = any(m in kw for m in accessory_markers)
    if is_accessory:
        keywords = [k for k in keywords if not any(h in k for h in host_markers)]

    scored = []
    # 词头之后紧跟这些字符时，说明查询词本身已是一个完整的词（"香水及花露水"），
    # 不再按"只是长词词头"打 70 折。
    _HEAD_BOUNDARY = set("及和与、，。：；（）()／/等或·-—")
    # 过泛的范畴类同义词：命中时只能当兜底（见下方 base 封顶）
    _GENERIC_SYNONYMS = {"食品", "商品", "制品", "用品", "其他", "材料", "物品"}
    for item in HS_CODES:
        # name 保持原样（它已由数据清洗修好，且尾部数字可能是商品名的一部分）；
        # description 按税率匹配清污。
        name = item.get("name", "")
        desc = _strip_rate_suffix(item.get("description", ""), item.get("import_tariff"))
        cat = item.get("category", "")
        hs4 = _get_hs4_desc(item.get("hs_code", ""))
        hs_code = item.get("hs_code", "")
        # 搜索文本包含HS编码，支持同义词直接映射到编码
        text = f"{name} {desc} {cat} {hs4} {hs_code}".lower()
        if not text.strip():
            continue
        # 字段加权：名字命中远高于税目注文命中。
        # 税目注文（hs4）是概括性法条文本，天然含大量商品词、甚至"……除外"的排他表述，
        # 若与名字同权竞争会造成误命中（实测"葡萄糖"被 2940 注文"…葡萄糖及果糖除外"抢走，
        # "香水"被 9616 注文"香水喷雾器…"抢走）。这里把 5 个字段分别打分再取最大。
        fields_w = ((name.lower(), 1.00), (desc.lower(), 0.85), ((hs4 or "").lower(), 0.70),
                    (cat.lower(), 0.65), (hs_code.lower(), 0.50))

        best_score = 0.0
        for search_kw in keywords:
            # HS编码前缀匹配（8位精确给400分，4位品目给60分）
            # 4位前缀分必须低于精确子串分（85/100），否则同义词中的数字前缀会霸榜，
            # 例如"鹅肝"→["肥肝","鹅","0207"]，0207前缀150分压过"肥肝"精确匹配85分，
            # 导致整只鸡0207.11排在真正命中的0207.53前面
            if search_kw.replace(".", "").isdigit() and hs_code.startswith(search_kw):
                if len(search_kw.replace(".", "")) >= 8:
                    score = 400.0
                else:
                    score = 60.0
                if score > best_score:
                    best_score = score
                continue

            # 提取连续2字词：仅保留纯汉字bigram。
            # 过滤数字/单位/符号bigram（如"2.5kg活鸡种用"→"2."".5""5k""kg""g活"，
            # "手机钢化膜"→"85""51""17""13""30"），避免命中无关编码（如"05"命中3105/4105/7105）
            bigrams = [search_kw[i:i + 2] for i in range(len(search_kw) - 1)
                       if " " not in search_kw[i:i + 2]
                       and _is_cjk(search_kw[i]) and _is_cjk(search_kw[i + 1])]

            # 1. 精确子串匹配（最高分）—— 逐字段打分后取最大，名字权重最高
            field_best = None
            for fval, fw in fields_w:
                if search_kw not in fval:
                    continue
                # "本章的什锦坚果或干果" 这类名字是章注交叉引用（全库 5 条），
                # 不是商品名本身：name 与 desc 都是同一段残片，两者都不享受商品名权重，
                # 退回按税目注文（hs4）打分，让真正的基础品目胜出。
                if name.startswith("本章") and fw >= 0.85:
                    continue
                fidx = fval.index(search_kw)
                base = 100.0 if search_kw == kw else 85.0
                # 同义词命中机械类条目降权：如"坚果"精确命中"水果、坚果或蔬菜加工机器"，
                # 但原查询词不含机械语义时，说明是泛词污染（应归0802松子而非8438加工机器）
                # 机械类条目降权：如"坚果"精确命中"水果、坚果或蔬菜加工机器"，
                # 但原查询词不含机械语义时，说明是"被加工的原料 vs 加工机器"的错位
                # （应归 0802 坚果而非 8438 加工机器）。
                # 判据只看**商品名**：category 里第十六类所有条目都写"机器、机械器具"，
                # 用整段 text 判会把小轿车/货船/剃须刀一起误伤。
                if any(m in name for m in ("机器", "设备", "装置", "机械")) \
                        and not any(m in kw for m in ("机器", "机", "器", "设备", "装置", "机械")):
                    base *= 0.4
                # 过泛同义词封顶：同义词扩展常把具体商品扩成"食品/制品/用品"这类范畴词，
                # 而库里有大量 name="食品级的" 的化学品，会让"坚果"命中 2835 次磷酸盐。
                # 这类同义词只能当兜底，不能与真正的商品名命中同权。
                if search_kw != kw and search_kw in _GENERIC_SYNONYMS:
                    base = min(base, 60.0)
                # 前缀截断降权：查询词是更长词条的**词头**时不算等价的精确命中。
                # 例："卷烟" 在 "卷烟滤嘴…" 里命中，若按满分处理，滤嘴(5601.2210)会
                # 压过真正的卷烟(2402.2000, 25%)。词头命中降到 70 分档。
                # 例外：紧跟连接词/标点时该词本身已完整（"香水及花露水"），不打折。
                if fidx == 0 and search_kw == kw and len(kw) >= 2:
                    rest = fval[len(search_kw):len(search_kw) + 2]
                    nxt = rest[:1]
                    # 只延伸 1 个汉字（亚麻纱→亚麻纱线）视为同一商品，不打折；
                    # 延伸 ≥2 个汉字（卷烟→卷烟滤嘴）才算"只是长词词头"降权；
                    # 紧跟连接词/标点（香水及花露水）说明本词已完整，也不打折。
                    if nxt and _is_cjk(nxt) and nxt not in _HEAD_BOUNDARY \
                            and len(rest) >= 2 and _is_cjk(rest[1]):
                        base = 70.0
                s = base - fidx * 0.05
                # 排他表述降权：命中的是"……除外/不包括"里的排除项，属反向证据
                # （2940 注文写"…葡萄糖及果糖除外"，恰恰说明葡萄糖不归 2940）
                if fw < 1.0 and "除外" in fval[max(0, fidx - 2):fidx + len(search_kw) + 10]:
                    s *= 0.5
                # 长名碎片降权：名字 ≥10 字且命中不在开头 → 多为叙述性长名偶然含该词
                # （"表面包覆钴化物的氢氧化镍"含"面包"）
                if fw == 1.00 and fidx > 0 and len(name) >= 10:
                    s *= 0.85
                s *= fw
                if field_best is None or s > field_best:
                    field_best = s
            if field_best is not None:
                score = field_best
            else:
                # 2. 连续2字词匹配
                matched_bigrams = [b for b in bigrams if b in text]
                if matched_bigrams:
                    base = 30.0 if search_kw == kw else 22.0
                    score = base + len(matched_bigrams) * 15.0
                    for i in range(len(search_kw) - 2):
                        trigram = search_kw[i:i + 3]
                        if " " not in trigram and all(_is_cjk(c) for c in trigram) and trigram in text:
                            score += 10.0
                else:
                    # 3. 单字匹配（兜底，大幅降权，要求匹配率高；仅汉字参与）
                    chars = [c for c in search_kw if _is_cjk(c)]
                    if not chars:
                        continue
                    unique_chars = set(chars)
                    matched = sum(1 for c in unique_chars if c in text)
                    ratio = matched / len(unique_chars)
                    if ratio < 0.7:  # 提高到70%，过滤更多噪音
                        continue
                    # 单字匹配大幅降权，同义词的单字匹配更低
                    base = 8.0 if search_kw == kw else 3.0
                    score = ratio * base

            if score > best_score:
                best_score = score

        # 低分过滤（合规拒答）：仅单bigram弱匹配（<60分，如"土壤"命中"土壤加热器"）视为无可靠依据，
        # 不硬凑编码，返回空列表走"无可靠依据，建议申请预裁定或咨询专业关务"分支
        if best_score >= 60.0:
            # 碎片名降权：数据里有一批 name 是被 PDF 解析切碎的注文残片
            # （以标点开头，如"，即中短粒米，粒长应小"），共 86 条，实测存在。
            # 它们不该排在正常商品名前面；这里只降权、不改数据，降权后仍可被检索到。
            if FRAGMENT_NAME_RE.match(name):
                best_score *= 0.85
            # 状态词重排（在 60 分门槛之后做，纯排序，不放宽/收紧召回）
            best_score = _state_adjust(hs_code, best_score, states)
            scored.append((best_score, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    # 同分档（相差 <0.1）内优先税号更小的：裸品类词（如"坚果"）会同时命中多个平级税目，
    # 各税目注文里都含该词（0802 鲜或干坚果 / 0811 冷冻 / 0813 什锦），原排序由字符位置决定，
    # 差 0.03 分纯属噪声。税则里基础品目在前，故同档取小税号。
    scored.sort(key=lambda x: (-round(x[0] / 0.1), x[1].get("hs_code", "")))

    # P0-1: HS编码层级过滤——同义词中含具体HS前缀时，优先返回同子目结果
    hs_prefixes = []
    for k in keywords:
        k_clean = k.replace(".", "")
        if k_clean.isdigit() and len(k_clean) >= 4:
            # 用前6位作为子目过滤前缀（如8471.3090 -> 847130），避免8位太精确导致匹配不足
            hs_prefixes.append(k_clean[:6])
    hs_prefixes = list(set(hs_prefixes))  # 去重
    if hs_prefixes and scored:
        # 先按前缀过滤
        prefix_matches = [(s, item) for s, item in scored
                          if any(item.get("hs_code", "").replace(".", "")[:6] == p for p in hs_prefixes)]
        # 前缀匹配≥2条就只用前缀结果，否则混合
        if len(prefix_matches) >= 2:
            scored = prefix_matches
        else:
            # 前缀匹配排前面，其余结果分数打5折
            non_prefix = [(s * 0.5, item) for s, item in scored
                           if not any(item.get("hs_code", "").replace(".", "")[:6] == p for p in hs_prefixes)]
            scored = prefix_matches + non_prefix
            scored.sort(key=lambda x: x[0], reverse=True)

    top = scored[:limit]
    return top if with_scores else [item for _, item in top]


# ========== API 接口 ==========

class ClassifyRequest(BaseModel):
    # 与前端 index.wxml 的 maxlength="200" 对齐：前端限制不是限制，
    # 直连接口可以发任意长文本，必须在服务端强制。
    description: str = Field(..., min_length=1, max_length=200)
    category: Optional[str] = Field(default="", max_length=64)


class ClassifyResponse(BaseModel):
    status: str
    recommendations: list
    disclaimer: str


@app.get("/")
def root():
    """服务信息：版本与数据版本都从这里取，便于确认"线上跑的是哪一版"。"""
    return {
        "status": "ok",
        "service": "税则通",
        "version": SERVICE_VERSION,
        "data": {
            "file": os.path.basename(HS_DATA_FILE),
            "records": len(HS_CODES),
            "non_zero_import_tariff": sum(1 for i in HS_CODES if i.get("import_tariff")),
            "meta": HS_DATA_META,
        },
    }


# 说明：语义缓存原先只挂在已下线的 AI 路径上，随 AI 代码一并移除，
# 因此该端点已无数据可报。保留路径但返回明确的"未启用"，避免任何调用方
# 把 404 误读成服务异常；也不再把进程内部计数无鉴权地暴露出去。
@app.get("/api/cache/stats")
def cache_stats_endpoint():
    return {"enabled": False, "reason": "semantic cache was tied to the removed AI path"}


# 匹配度（confidence）真实化：不再按排序下标编造，改为由"匹配质量"推导。
# 输入是 fuzzy_search 给出的原始检索分（best_score），归一化到 0-1：
#   >=100 分  : 查询词在商品名/描述中**精确子串**命中     -> 0.85-0.99
#   60-100 分 : 同义词精确命中，或多个 bigram/trigram 命中 -> 0.55-0.85
#   <60 分    : 不返回（fuzzy_search 已过滤，视为无可靠依据）
# 所以置信度与"命中几条字段、命中多长片段"单调相关，而不再与名次相关。
_CONF_EXACT_FLOOR, _CONF_EXACT_CEIL = 0.85, 0.99
_CONF_PARTIAL_FLOOR, _CONF_PARTIAL_CEIL = 0.55, 0.85


def _confidence_from_score(score) -> float:
    """把检索原始分折算为 0-1 匹配度。分数越低越接近下限，越高越接近上限。"""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return _CONF_PARTIAL_FLOOR
    if s >= 100.0:
        # 精确命中：100 分即上限，再多命中只是同档
        conf = _CONF_EXACT_CEIL
    elif s >= 60.0:
        span = _CONF_PARTIAL_CEIL - _CONF_PARTIAL_FLOOR
        conf = _CONF_PARTIAL_FLOOR + span * (s - 60.0) / 40.0
    else:
        conf = _CONF_PARTIAL_FLOOR * max(s, 0.0) / 60.0
    return round(conf, 2)


def _conf_pct(conf) -> int:
    """匹配度的整数百分比，供界面展示（如 0.99 -> 99）。"""
    try:
        return int(round(float(conf) * 100))
    except (TypeError, ValueError):
        return 0


def _build_basis(item: dict) -> str:
    """归类依据：品目号 + 品目条文 + 税则号列。

    说明（诚实版）：这是**检索到的税则条文位置**，不是归类总规则(GRI)或章注/类注
    的推理结论。本服务没有 GRI 规则引擎，所以措辞用"对应品目条文"，
    不再用会被读成法律依据的写法。
    """
    hs_code = item.get("hs_code", "")
    hs4 = hs_code.replace(".", "")[:4]
    hs4_desc = _get_hs4_desc(hs_code)
    if hs4_desc:
        return f"依据：税则号列{hs_code}，对应品目{hs4}（{hs4_desc}）"
    return f"依据：税则号列{hs_code}"


def _format_search_results(items: list, description: str, scores: dict = None) -> list:
    """将 fuzzy_search 结果格式化为前端可展示的结构。

    scores: {id(item): 检索原始分}，来自 fuzzy_search(with_scores=True)。
    缺省时置信度取保守下限，绝不编造高档位。
    """
    results = []
    for idx, item in enumerate(items[:5]):
        score = scores.get(id(item)) if scores else None
        base_name = item.get("name", "")
        hs4 = _get_hs4_desc(item.get("hs_code", ""))
        display_name = f"{base_name}（{hs4}）" if hs4 and len(base_name) <= 4 else base_name
        desc = _strip_rate_suffix(item.get("description", ""), item.get("import_tariff"))
        conf = _confidence_from_score(score) if score is not None else _CONF_PARTIAL_FLOOR
        tariff = item.get("import_tariff") or 0
        vat = item.get("vat_rate") or 0
        comprehensive = ((1 + tariff/100) * (1 + vat/100) - 1) * 100
        results.append({
            "hs_code": item["hs_code"],
            "name": display_name,
            "confidence": conf,
            "confidence_pct": _conf_pct(conf),
            "match_score": round(float(score), 1) if score is not None else None,
            "reason": f"税则数据库匹配：{desc or base_name}",
            "basis": _build_basis(item),
            "tax": {
                "import_tariff": f"{tariff}%",
                "vat_rate": f"{vat}%",
                "comprehensive_rate": f"{comprehensive:.1f}%"
            }
        })
    return results


@app.post("/api/classify")
def classify(payload: ClassifyRequest, request: Request):
    """商品归类接口：输入商品描述，返回HS Code候选（纯税则数据库检索）"""
    _rate_limit(request)
    if not payload.description.strip():
        raise HTTPException(status_code=400, detail="商品描述不能为空")

    # 宽泛词先于预置表：通用名对应多个品目时，返回多候选请用户补充信息，
    # 不给单一"确定答案"（预置表仍服务于更具体的描述）
    broad = _check_broad(payload.description)
    if broad:
        cands = _broad_candidates(payload.description, broad["prefixes"])
        if len(cands) >= 2:
            return {
                "status": "need_clarification",
                "mode": "broad",
                "recommendations": _format_search_results(cands, payload.description),
                "clarification": {
                    "question": broad["question"],
                    "note": broad["note"],
                    "hint": "补充用途/成分后重新查询，可得到确定的税号。",
                },
                "disclaimer": "该品名为通用名，对应多个税号，需补充信息才能确定归类；"
                              "本结果仅供参考，不构成申报依据，最终归类以海关核定为准。",
            }

    # P1-1: 先查常见商品预置表，命中直接返回
    preset = _check_preset(payload.description)
    if preset:
        # None税率容错（历史占位条目无税率数据时按0计算并标注）
        t = preset["import_tariff"] or 0
        v = preset["vat_rate"] or 0
        c = preset["consumption_tax"] or 0
        comprehensive = ((1 + t/100) * (1 + v/100) * (1 + c/100) - 1) * 100
        return {
            "status": "success",
            "mode": "preset",
            "recommendations": [{
                "hs_code": preset["hs_code"],
                "name": preset["name"],
                "confidence": preset["confidence"],
                "confidence_pct": _conf_pct(preset["confidence"]),
                # 预置表条目是人工核对过的精确命中，检索分按满分标注，
                # 字段与 search 模式保持一致，前端无需两套逻辑。
                "match_score": 100.0,
                "reason": preset["reason"],
                "basis": preset["basis"],
                "tax": {
                    "import_tariff": f"{t}%",
                    "vat_rate": f"{v}%",
                    "comprehensive_rate": f"{comprehensive:.1f}%"
                }
            }],
            "disclaimer": "本结果基于常见商品预置归类，仅供参考，不构成申报依据，最终申报责任由申报主体承担。最终归类以海关核定为准。"
        }

    scored_items = fuzzy_search(payload.description, limit=8, with_scores=True)
    scores = {id(item): score for score, item in scored_items}
    results = _format_search_results([item for _, item in scored_items], payload.description, scores)

    if results:
        return {
            "status": "success",
            "mode": "search",
            "recommendations": results,
            "disclaimer": "本结果基于税则数据库关键词匹配，仅供参考，不构成申报依据，最终申报责任由申报主体承担。最终归类以海关核定为准。"
        }
    else:
        return {
            "status": "not_found",
            "mode": "search",
            "recommendations": [],
            "disclaimer": "无可靠依据，建议申请预裁定或咨询专业关务。未在税则数据库中找到匹配商品，可尝试更通用的关键词或补充商品材质、用途、加工工艺。",
            "feedback": {
                "email": "hq15012670635@163.com",
                "message": "请将您查询的商品名称发送到上述邮箱，我们会尽快补充数据。"
            }
        }


@app.post("/api/classify/ai")
def classify_ai():
    """AI 归类接口已下线（410 Gone）。

    个人主体小程序不得提供 AI 问答（深度合成）能力；服务端不再持有任何大模型
    密钥，也不再向第三方发送用户输入。前端从未调用该路由（全小程序只调用
    /api/classify），保留 410 是为了让旧版客户端或历史调用方得到明确答复，
    而不是 404 那样无法区分"路径写错"和"功能已下线"。
    """
    raise HTTPException(
        status_code=410,
        detail="AI 归类功能已下线，请使用 /api/classify（税则数据库检索）。",
    )


@app.get("/api/hs/{hs_code}")
def get_hs(hs_code: str, request: Request = None):
    """根据 HS Code 查询详细信息（有限流，防止 8972 条被一次性爬走）"""
    if request is not None:
        _rate_limit(request)
    for item in HS_CODES:
        if item["hs_code"] == hs_code:
            return item
    raise HTTPException(status_code=404, detail="未找到该HS Code")


@app.get("/api/categories")
def get_categories(request: Request = None):
    """获取商品类别列表（有限流）"""
    if request is not None:
        _rate_limit(request)
    categories = list(set(item["category"] for item in HS_CODES))
    return {"categories": sorted(categories)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
