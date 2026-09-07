"""
海关商品归类 AI 助手 - FastAPI 后端
提供商品归类 API 接口，调用 LangChain Agent + MCP 工具
"""
import os
import json
from typing import Optional
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

# 加载 .env
load_dotenv()

# 配置
app = FastAPI(title="海关商品归类 AI 助手", version="1.0.0")

# 阿里云百炼 API 配置（OpenAI 兼容）
LLM_CONFIG = {
    "base_url": os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "api_key": os.getenv("LLM_API_KEY", ""),
    "model": os.getenv("LLM_MODEL", "qwen3.8-flash"),
}

# CORS（微信小程序域名白名单，生产环境收窄）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 开发阶段放开，生产改为小程序域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 加载 HS Code 数据（完整8972条2026税则）
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
HS_DATA_FILE = os.path.join(DATA_DIR, "hs_codes_full.json")
if not os.path.exists(HS_DATA_FILE):
    HS_DATA_FILE = os.path.join(DATA_DIR, "hs_codes.json")
with open(HS_DATA_FILE, "r", encoding="utf-8") as f:
    HS_CODES = json.load(f)


# ========== 工具定义（LangChain Tool 格式） ==========

@tool
def search_hs_code(keyword: str, category: str = "") -> str:
    """根据商品描述关键词搜索HS Code（海关编码）。输入商品描述，返回匹配的HS Code、税率、监管条件。"""
    results = []
    keyword_lower = keyword.lower()
    for item in HS_CODES:
        text = f"{item['name']} {item['description']} {item['category']}".lower()
        if keyword_lower in text:
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
        return json.dumps({"status": "not_found", "message": f"未找到与'{keyword}'相关的HS Code"}, ensure_ascii=False)
    return json.dumps({"status": "success", "count": len(results), "results": results[:5]}, ensure_ascii=False)


@tool
def get_hs_detail(hs_code: str) -> str:
    """根据HS Code获取商品详细信息，包括税率、监管条件、归类依据。"""
    for item in HS_CODES:
        if item["hs_code"] == hs_code:
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
    return json.dumps({"status": "not_found", "message": f"未找到HS Code '{hs_code}'"}, ensure_ascii=False)


@tool
def calculate_tax(hs_code: str, customs_value: float) -> str:
    """根据HS Code和完税价格计算进口税费（关税+增值税+消费税）。"""
    for item in HS_CODES:
        if item["hs_code"] == hs_code:
            tariff = customs_value * item["import_tariff"] / 100
            if item["consumption_tax"] > 0:
                consumption_base = (customs_value + tariff) / (1 - item["consumption_tax"]/100)
                consumption = consumption_base * item["consumption_tax"] / 100
            else:
                consumption = 0
            vat = (customs_value + tariff + consumption) * item["vat_rate"] / 100
            total = tariff + consumption + vat
            return json.dumps({
                "status": "success",
                "tax_breakdown": {
                    "import_tariff": round(tariff, 2),
                    "consumption_tax": round(consumption, 2),
                    "vat": round(vat, 2),
                    "total": round(total, 2)
                }
            }, ensure_ascii=False)
    return json.dumps({"status": "not_found", "message": f"未找到HS Code '{hs_code}'"}, ensure_ascii=False)


# ========== LLM + Tool Calling（不依赖 langchain.agents，避免numpy编译问题） ==========

SYSTEM_PROMPT = """你是一个专业的海关商品归类助手。用户会输入商品描述，你必须调用工具查询后给出HS Code推荐。

【强制规则】
1. 收到任何商品描述，必须先调用 search_hs_code 工具搜索，禁止直接回复
2. 搜索到结果后，调用 get_hs_detail 获取详细信息
3. 最后输出纯JSON格式的推荐结果，不要任何解释文字、不要markdown代码块

输出格式（纯JSON）：
{"recommendations":[{"hs_code":"编码","name":"商品名称","confidence":0.95,"reason":"归类理由","tax":{"import_tariff":"0%","vat_rate":"13%","comprehensive_rate":"13.0%"}}],"disclaimer":"本结果仅供参考，实际归类以海关核定为准"}

注意：搜索不到就如实说，不要编造编码。"""

def run_classifier(description: str) -> dict:
    """用 LLM + 工具调用实现商品归类（手动实现tool calling循环）"""
    llm = ChatOpenAI(
        model=LLM_CONFIG["model"],
        temperature=0.1,
        api_key=LLM_CONFIG["api_key"],
        base_url=LLM_CONFIG["base_url"],
    )
    
    tools = [search_hs_code, get_hs_detail, calculate_tax]
    llm_with_tools = llm.bind_tools(tools)
    
    messages = [
        ("system", SYSTEM_PROMPT),
        ("human", f"请查询以下商品的HS Code并输出JSON结果：{description}"),
    ]
    
    # 最多3轮工具调用
    for _ in range(3):
        response = llm_with_tools.invoke(messages)
        messages.append(response)
        
        if not response.tool_calls:
            # 没有工具调用，返回最终结果
            try:
                return json.loads(response.content)
            except json.JSONDecodeError:
                return {"status": "parse_error", "raw_output": response.content}
        
        # 执行工具调用
        for tool_call in response.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]
            tool_id = tool_call["id"]
            
            tool_map = {t.name: t for t in tools}
            if tool_name in tool_map:
                try:
                    tool_result = tool_map[tool_name].invoke(tool_args)
                except Exception as e:
                    tool_result = json.dumps({"error": str(e)}, ensure_ascii=False)
            else:
                tool_result = json.dumps({"error": f"Unknown tool: {tool_name}"}, ensure_ascii=False)
            
            messages.append(ToolMessage(content=tool_result, tool_call_id=tool_id))
    
    # 超过3轮，强制返回
    final_response = llm.invoke(messages)
    try:
        return json.loads(final_response.content)
    except json.JSONDecodeError:
        return {"status": "max_iterations", "raw_output": final_response.content}


# ========== API 接口 ==========

class ClassifyRequest(BaseModel):
    description: str
    category: Optional[str] = ""


class ClassifyResponse(BaseModel):
    status: str
    recommendations: list
    disclaimer: str


@app.get("/")
def root():
    return {"status": "ok", "service": "海关商品归类AI助手", "version": "1.0.0"}


@app.post("/api/classify")
def classify(request: ClassifyRequest):
    """商品归类接口：输入商品描述，返回HS Code推荐"""
    if not request.description.strip():
        raise HTTPException(status_code=400, detail="商品描述不能为空")
    
    try:
        result = run_classifier(request.description)
        return result
    except Exception as e:
        # LLM 调用失败时的降级方案：直接关键词搜索
        results = []
        keyword = request.description.lower()
        for item in HS_CODES:
            text = f"{item['name']} {item['description']}".lower()
            if any(w in text for w in keyword.split()):
                results.append({
                    "hs_code": item["hs_code"],
                    "name": item["name"],
                    "confidence": 0.6,
                    "reason": f"关键词匹配：{item['description']}",
                    "tax": {
                        "import_tariff": f"{item['import_tariff']}%",
                        "vat_rate": f"{item['vat_rate']}%",
                        "comprehensive_rate": f"{((1+item['import_tariff']/100)*(1+item['vat_rate']/100)-1)*100:.1f}%"
                    }
                })
        return {
            "status": "fallback",
            "recommendations": results[:3],
            "disclaimer": "AI服务暂不可用，当前为关键词匹配结果，仅供参考"
        }


@app.get("/api/hs/{hs_code}")
def get_hs(hs_code: str):
    """根据 HS Code 查询详细信息"""
    for item in HS_CODES:
        if item["hs_code"] == hs_code:
            return item
    raise HTTPException(status_code=404, detail="未找到该HS Code")


@app.get("/api/categories")
def get_categories():
    """获取商品类别列表"""
    categories = list(set(item["category"] for item in HS_CODES))
    return {"categories": sorted(categories)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
