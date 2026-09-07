# 归类通 - 海关商品归类 AI 助手

> 「与AI共生」2026微信小程序开发大赛参赛作品

## 项目简介

用户输入商品描述，AI 自动推荐 HS Code（海关编码）、对应税率、监管条件及归类依据，帮助中小企业和报关行快速完成商品归类。

## 技术架构

```
微信小程序（前端）
    ↓
FastAPI 后端（Python）
    ↓
LangChain Agent（推理核心）
    ├── 工具1: HS Code 搜索
    ├── 工具2: 商品详情查询
    └── 工具3: 税费计算
    ↓
DeepSeek LLM
```

## 目录结构

```
hs-code-ai-assistant/
├── data/
│   └── hs_codes.json          # HS Code 数据库（50条常见商品）
├── mcp/
│   └── hs_code_server.py      # MCP 工具服务器
├── backend/
│   └── main.py                # FastAPI 后端 + LangChain Agent
├── miniprogram/               # 微信小程序前端
│   ├── pages/
│   │   ├── index/             # 首页（搜索）
│   │   ├── result/            # 结果页
│   │   └── history/           # 历史记录
│   ├── app.js
│   ├── app.json
│   ├── app.wxss
│   └── sitemap.json
├── requirements.txt
└── README.md
```

## 快速开始

### 1. 安装后端依赖

```bash
cd hs-code-ai-assistant
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
export DEEPSEEK_API_KEY="your-api-key"
```

### 3. 启动后端

```bash
cd backend
python main.py
# 服务运行在 http://localhost:8000
```

### 4. 启动 MCP 工具（可选，独立使用时）

```bash
cd mcp
python hs_code_server.py
```

### 5. 微信小程序

1. 用微信开发者工具打开 `miniprogram/` 目录
2. 修改 `app.js` 中的 `apiBase` 为你的后端地址
3. 编译运行

## API 接口

### POST /api/classify
商品归类接口

**请求：**
```json
{
  "description": "不锈钢厨房用勺子"
}
```

**响应：**
```json
{
  "recommendations": [
    {
      "hs_code": "7323.93.00",
      "name": "不锈钢制厨房用具",
      "confidence": 0.9,
      "reason": "归类依据...",
      "tax": {
        "import_tariff": "8%",
        "vat_rate": "13%",
        "comprehensive_rate": "22.0%"
      }
    }
  ],
  "disclaimer": "本结果仅供参考"
}
```

### GET /api/hs/{hs_code}
查询 HS Code 详情

### GET /api/categories
获取商品类别列表

## 数据说明

当前包含 50 条常见商品 HS Code 数据，覆盖：
- 机电产品（电脑、手机、显示器等）
- 纺织服装（T恤、衬衫、鞋等）
- 食品饮料（咖啡、茶、巧克力等）
- 医药化工（药品、化妆品、维生素等）
- 家居日用（家具、灯具、餐具等）
- 文化办公（图书、文具、打印机等）

后续可扩充至 500+ 条。

## 免责声明

本工具仅供学习和参考使用，商品归类结果不构成正式报关依据，实际归类以海关核定为准。
