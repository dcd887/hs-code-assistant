"""
税则通 - MCP 工具服务器
提供 HS Code 查询工具
"""
import json
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP

# 数据文件路径
DATA_DIR = Path(__file__).parent.parent / "data"
HS_CODE_FILE = DATA_DIR / "hs_codes.json"

# 加载 HS Code 数据
def load_hs_codes():
    with open(HS_CODE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

HS_CODES = load_hs_codes()

# 创建 MCP 服务器
mcp = FastMCP("hs-code-assistant")


@mcp.tool()
def search_hs_code(keyword: str, category: str = "") -> str:
    """
    根据商品描述关键词搜索 HS Code（海关编码）。
    
    Args:
        keyword: 商品描述关键词，如"笔记本电脑"、"纯棉T恤"、"不锈钢锅"
        category: 可选，商品类别筛选，如"机电产品"、"纺织服装"、"食品饮料"
    
    Returns:
        JSON格式的搜索结果，包含匹配的HS Code、商品名称、税率、监管条件等
    """
    results = []
    keyword_lower = keyword.lower()
    
    for item in HS_CODES:
        # 关键词匹配：名称、描述、类别
        text = f"{item['name']} {item['description']} {item['category']}".lower()
        if keyword_lower in text:
            # 如果指定了类别，进一步筛选
            if category and category not in item["category"]:
                continue
            results.append({
                "hs_code": item["hs_code"],
                "name": item["name"],
                "category": item["category"],
                "description": item["description"],
                "import_tariff": item["import_tariff"],
                "vat_rate": item["vat_rate"],
                "consumption_tax": item["consumption_tax"],
                "supervision": item["supervision"]
            })
    
    if not results:
        # 模糊匹配：拆分关键词逐个匹配
        words = keyword_lower.split()
        for item in HS_CODES:
            text = f"{item['name']} {item['description']} {item['category']}".lower()
            if any(w in text for w in words):
                if category and category not in item["category"]:
                    continue
                results.append({
                    "hs_code": item["hs_code"],
                    "name": item["name"],
                    "category": item["category"],
                    "description": item["description"],
                    "import_tariff": item["import_tariff"],
                    "vat_rate": item["vat_rate"],
                    "consumption_tax": item["consumption_tax"],
                    "supervision": item["supervision"]
                })
    
    if not results:
        return json.dumps({
            "status": "not_found",
            "message": f"未找到与'{keyword}'相关的HS Code，请尝试更通用的关键词，或联系专业报关员确认。",
            "suggestions": ["尝试使用商品材质关键词，如'棉制'、'不锈钢'、'塑料'", "尝试使用商品用途关键词，如'厨房用'、'办公用'"]
        }, ensure_ascii=False, indent=2)
    
    return json.dumps({
        "status": "success",
        "count": len(results),
        "results": results[:5]  # 最多返回5个候选
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def get_hs_detail(hs_code: str) -> str:
    """
    根据 HS Code 获取商品详细信息，包括税率、监管条件、归类依据。
    
    Args:
        hs_code: 海关编码，如"8471.30.00"
    
    Returns:
        JSON格式的详细信息
    """
    for item in HS_CODES:
        if item["hs_code"] == hs_code:
            # 计算综合税率
            comprehensive_rate = (1 + item["import_tariff"]/100) * (1 + item["vat_rate"]/100) * (1 + item["consumption_tax"]/100) - 1
            return json.dumps({
                "status": "success",
                "hs_code": item["hs_code"],
                "name": item["name"],
                "category": item["category"],
                "description": item["description"],
                "tax": {
                    "import_tariff": f"{item['import_tariff']}%",
                    "vat_rate": f"{item['vat_rate']}%",
                    "consumption_tax": f"{item['consumption_tax']}%",
                    "comprehensive_rate": f"{comprehensive_rate*100:.1f}%"
                },
                "supervision": item["supervision"],
                "classification_basis": item["basis"]
            }, ensure_ascii=False, indent=2)
    
    return json.dumps({
        "status": "not_found",
        "message": f"未找到HS Code '{hs_code}'的详细信息"
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def calculate_tax(hs_code: str, customs_value: float) -> str:
    """
    根据 HS Code 和完税价格计算进口税费。
    
    Args:
        hs_code: 海关编码
        customs_value: 完税价格（人民币元）
    
    Returns:
        JSON格式的税费计算结果
    """
    for item in HS_CODES:
        if item["hs_code"] == hs_code:
            tariff = customs_value * item["import_tariff"] / 100
            # 消费税计税价格 = (完税价格 + 关税) / (1 - 消费税税率)
            if item["consumption_tax"] > 0:
                consumption_base = (customs_value + tariff) / (1 - item["consumption_tax"]/100)
                consumption = consumption_base * item["consumption_tax"] / 100
            else:
                consumption = 0
            vat = (customs_value + tariff + consumption) * item["vat_rate"] / 100
            total = tariff + consumption + vat
            
            return json.dumps({
                "status": "success",
                "hs_code": hs_code,
                "customs_value": customs_value,
                "tax_breakdown": {
                    "import_tariff": round(tariff, 2),
                    "consumption_tax": round(consumption, 2),
                    "vat": round(vat, 2),
                    "total": round(total, 2)
                },
                "note": "计算结果仅供参考，实际税费以海关核定为准"
            }, ensure_ascii=False, indent=2)
    
    return json.dumps({
        "status": "not_found",
        "message": f"未找到HS Code '{hs_code}'"
    }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    print("税则通 MCP Server 启动中...")
    print(f"已加载 {len(HS_CODES)} 条 HS Code 数据")
    mcp.run()
