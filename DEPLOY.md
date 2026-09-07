# 微信云托管部署指南

## 前置条件
- 小程序已完成微信认证（个人主体30元）
- 已开通微信云托管（免费额度足够MVP）

## 部署步骤

### 1. 开通云托管
1. 微信开发者工具 → 「云开发」→「云托管」
2. 按提示开通，选择「按量付费」（有免费额度，MVP阶段不花钱）
3. 创建服务，服务名：`hs-code-api`

### 2. 配置环境变量
在云托管控制台 → 服务 → 版本管理 → 部署时配置环境变量：

| 变量名 | 值 |
|---|---|
| LLM_API_KEY | sk-6e73bd57b8b749f38bb4fab4f2c632c6 |
| LLM_BASE_URL | https://dashscope.aliyuncs.com/compatible-mode/v1 |
| LLM_MODEL | qwen-plus |

> 注意：环境变量在云托管控制台配置，不要写进代码或Dockerfile。

### 3. 部署
方式一（推荐）：微信开发者工具内上传
1. 开发者工具 → 云托管 → 服务管理 → 上传代码
2. 选择项目根目录（含Dockerfile）
3. 端口填8000
4. 等待构建部署（约3-5分钟）

方式二：CLI部署
```bash
npm install -g @cloudbase/cli
tcb login
tcb service deploy --name hs-code-api --source .
```

### 4. 获取访问地址
部署成功后，云托管会给一个默认域名，格式如：
`https://hs-code-api-xxxxxxx.ap-shanghai.run.tcloudbase.com`

### 5. 小程序端配置
修改 `miniprogram/app.js`：
```js
apiBase: 'https://hs-code-api-xxxxxxx.ap-shanghai.run.tcloudbase.com'
```

或者用 `wx.cloud.callContainer()` 直接调用（推荐，免域名配置）：
```js
wx.cloud.callContainer({
  path: '/api/classify',
  method: 'POST',
  data: { description },
  success: res => { ... }
})
```

### 6. 验证
访问 `https://你的域名/` 应返回：
```json
{"status":"ok","service":"海关商品归类AI助手","version":"1.0.0"}
```

## 免费额度
- 云托管：每月一定免费额度（GB-s、请求次数），MVP阶段足够
- 超出后按量付费，成本很低

## 回滚
云托管支持版本管理，出问题一键回滚到上一版本。
