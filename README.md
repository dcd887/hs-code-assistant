# 税则通 - 商品编码与税率查询工具

微信小程序，输入商品名称即可查询 HS 编码（海关商品编号）、进口关税、增值税率及监管条件。数据基于财政部《中华人民共和国进出口税则（2026）》，覆盖全部 8,972 个税则号列。

## 功能

- 商品名称模糊搜索，返回最匹配的 HS 编码
- 显示最惠国税率、增值税率、综合税率
- 归类依据说明
- 历史查询记录
- 一键复制 HS 编码

## 技术架构

```
微信小程序（前端，WXML/WXSS/JS）
    ↓ HTTP API
FastAPI 后端（Python）
    ↓ Tool Calling
LangChain + 大模型 API（阿里云百炼）
    ├── 工具1: HS Code 全文搜索（8,972条）
    ├── 工具2: 商品详情查询
    └── 工具3: 税费计算
    ↓
本地税则数据库（JSON）
```

## 目录结构

```
hs-code-ai-assistant/
├── data/
│   ├── hs_codes_full.json           # 完整税则数据库（8,972条）
│   ├── hs_codes.json                # 精选常见商品（60条）
│   └── 海关商品归类知识库_2026.md    # 知识库文档
├── backend/
│   ├── main.py                      # FastAPI 后端
│   ├── requirements.txt
│   └── .env                         # 环境变量（API Key，不入库）
├── mcp/
│   └── hs_code_server.py            # MCP 工具服务器（可选）
├── miniprogram/                     # 微信小程序前端
│   ├── pages/
│   │   ├── index/                   # 首页（搜索）
│   │   ├── result/                  # 结果页
│   │   └── history/                 # 历史记录
│   ├── app.js / app.json / app.wxss
│   └── project.config.json
├── Dockerfile                       # 微信云托管部署
├── DEPLOY.md                        # 部署指南
└── README.md
```

## 快速开始

### 1. 安装后端依赖

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 配置环境变量

在 `backend/.env` 中配置：

```
LLM_API_KEY=your-api-key
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus
```

### 3. 启动后端

```bash
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 4. 微信小程序

1. 用微信开发者工具打开 `miniprogram/` 目录
2. AppID: `wxb044bbb96b2b11a7`
3. 开发阶段在 `app.js` 中配置 `apiBase` 为后端地址
4. 详情 → 本地设置 → 勾选「不校验合法域名」

## API 接口

### POST /api/classify

商品归类查询。

**请求：**
```json
{ "description": "智能手机" }
```

**响应：**
```json
{
  "recommendations": [
    {
      "hs_code": "8517.1300",
      "name": "智能手机",
      "confidence": 0.95,
      "reason": "智能手机属于蜂窝网络用电话机，归入8517.13",
      "tax": {
        "import_tariff": "0%",
        "vat_rate": "13%",
        "comprehensive_rate": "13.0%"
      }
    }
  ],
  "disclaimer": "本结果仅供参考，实际归类以海关核定为准"
}
```

### GET /api/hs/{hs_code}
查询指定 HS 编码详情。

### GET /api/categories
获取商品类别列表。

## 数据来源

- 财政部《中华人民共和国进出口税则（2026）》（1492页，8,972个税则号列）
- 海关总署公告2025年第260号（2026年关税调整方案）
- 海关总署公告2026年第15号（9%增值税税率商品表）
- 海关总署公告2026年第126号（电池产品编码）

## 部署

详见 [DEPLOY.md](./DEPLOY.md)（微信云托管 + Docker）。

## 免责声明

本工具为个人开发的查询辅助工具，非海关官方产品。查询结果仅供参考，不构成正式报关依据，实际归类以海关核定为准。
