# AIGC 图片平台 V1

这是一个可独立运行的最小闭环：

- 表情包、手机壁纸、电脑壁纸三类模板；
- 按模块使用推荐尺寸，也支持 256–4096 像素、0.25–4 比例的自定义尺寸；
- Doubao-Seedream、SenseNova U1.5 Lite、Qwen-Image 可作为图片模型，SenseNova 6.8 Flash Lite 默认标记为理解/审核模型；
- 模板自带提示词骨架，用户选择模板后可以继续修改；
- 外部模板先进入候选区，审核通过后才进入正式模板库；
- 候选模板可以携带提示词骨架，审核通过后会原样进入正式模板库；
- 候选审核会对明显的暴力/血腥、成人/裸露词做规则预检提示，但不会自动拒绝，最终仍由管理员审核；
- 记录模块、模型、供应商、模板、用户的用量与成本。
- 用户端的模型下拉框从后台配置动态读取，只展示已启用的生图模型；没有可用模型时会禁用生成并提示联系管理员。
- SenseNova U1.5 Lite 已加入后台模型目录，当前默认是开发测试 Mock；如果只启用了 SenseNova 6.8 Flash Lite，用户端会提示它是理解/审核模型，不能直接生图。

## 启动

```bash
cd projects/aigc-content-platform
python3 app.py
```

打开 <http://127.0.0.1:8000>。默认生图是本地 SVG 模拟，不需要图片模型密钥。页面会明确显示“开发测试模型 · Mock”、供应商名称和模型 ID；因此选择“千问 · Qwen-Image”时，当前只能证明配置的是千问模型元数据，不能证明已经调用了千问 API。只有接入真实供应商适配器并看到真实调用回执后，才会标记为真实调用。

启动时会自动读取当前目录、项目目录或上级项目目录中的 `.env`（按此顺序取第一个存在的文件）；同名的系统环境变量优先，不会被 `.env` 覆盖。真实模型的密钥只进入进程环境，不写入数据库，也不会返回前端。例如商汤使用 `SENSENOVA_API_KEY=你的商汤 TokenPlan 密钥`。

页面分成两个独立入口：

- 用户端：`/`，只负责模板选择、提示词、尺寸和生图。
- 管理工作台：`/admin`，负责用量、模型配置、模板审核和发布。

两端共用同一套 API 和 SQLite 台账，但导航与入口完全分开，普通创作用户不会看到管理能力。

## 切换到千问真实生图

默认的 `qwen-image` 也是 Mock。要切换到真实调用，需要在启动进程中配置千问密钥和兼容接口地址，并在管理端把该模型的“运行适配器”改成“千问 HTTP”：

```bash
export QWEN_API_KEY='你的千问 API Key'
export QWEN_ENDPOINT='https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/images/generations'
python3 app.py
```

也可以通过管理接口配置适配器和远端模型 ID：

```bash
curl -X POST http://127.0.0.1:8000/api/admin/models/qwen-image \
  -H 'content-type: application/json' \
  -d '{"adapter":"qwen_http","remote_model_id":"qwen-image-3.0-pro"}'
```

真实调用成功后，生成结果会显示“真实调用”，并保存为 PNG；缺少密钥或接口地址时会直接报配置错误，不会降级成假图。API Key 只从环境变量读取，不写入 SQLite。

后台“接口地址”字段只接受 `http(s)` URL；如果把 API Key 误填到这里，接口会直接拒绝并提示改用环境变量。

如果供应商响应包含 `usage.total_tokens` 或 `input_tokens`/`output_tokens`，系统会写入真实 Token 数；供应商返回成本时优先使用供应商成本，否则使用后台模型配置的单次成本。

## 切换到豆包 Seedream 真实生图

内置 `doubao-seedream` 默认仍是开发测试 Mock。要调用火山方舟的 Doubao Seedream，请先在方舟控制台开通对应模型，并把 API Key 写入启动环境（不要写入 SQLite 或前端）：

```bash
export ARK_API_KEY='你的方舟 API Key'
python3 app.py
```

在管理端“模型配置”中选择 `豆包 Seedream HTTP`，填写控制台实际提供的远端模型 ID（例如 `doubao-seedream-5-0-lite-260128`），接口地址可留空，默认使用 `https://ark.cn-beijing.volces.com/api/v3/images/generations`；密钥环境变量名默认使用 `ARK_API_KEY`，也可以在页面改成其他环境变量名。缺少密钥时，“检查配置”会提示待配置，生成不会静默退回 Mock。

豆包适配器发送标准图片生成请求，支持提示词、尺寸和参考图，并会把返回的 URL 或 Base64 图片保存到本地 PNG。接口字段和模型 ID 以火山方舟官方图片生成 API 为准：<https://docs.volcengine.com/docs/ark/image-generation-api?lang=zh&redirect=1>。

模型配置中的每日限额按“用户 + 模型”统计当天成功调用次数；设置为 `0` 表示不限制，达到限额后会拒绝新的生成请求。

用户端支持上传 PNG/JPG/WebP 参考图（最大 10 MB）；真实 Qwen 调用会以 Base64 `image` 参数发送，Mock 只记录已附加参考图的状态。

用户端也支持填写“不希望出现”的内容；真实 Qwen 调用会以 `negative_prompt` 参数发送，Mock 会在回执中标记负面提示词已附加。

用户端每天默认有 3 张免费图片额度，额度只统计当天成功生成的图片；失败请求不会扣除。可通过 `GET /api/quota?user_id=demo` 查看，用户也可以在“我的生成”中删除自己的图片。后台模型每日限额仍独立生效。

## 常用接口

```bash
curl http://127.0.0.1:8000/api/health
curl 'http://127.0.0.1:8000/api/templates?module=emoji'
curl 'http://127.0.0.1:8000/api/generations?user_id=demo'
curl http://127.0.0.1:8000/api/admin/templates
curl http://127.0.0.1:8000/api/admin/connectors
curl -X POST http://127.0.0.1:8000/api/admin/connectors/doubao/sync \
  -H 'content-type: application/json' -H 'X-Admin-Role: admin' \
  -d '{"actor":"admin"}'
curl -H 'X-Admin-Role: admin' http://127.0.0.1:8000/api/admin/templates/review-summary
curl -H 'X-Admin-Role: admin' 'http://127.0.0.1:8000/api/admin/review-runs?limit=10'
curl -X POST http://127.0.0.1:8000/api/admin/review-runs/run \
  -H 'content-type: application/json' -H 'X-Admin-Role: admin' \
  -d '{"module":"emoji","actor":"admin"}'
curl -X POST http://127.0.0.1:8000/api/admin/templates/candidates/batch \
  -H 'content-type: application/json' -H 'X-Admin-Role: admin' \
  -d '{"connector_id":"","items":[{"module":"emoji","title":"猫猫","source_url":"https://example.com/cat"}]}'
curl http://127.0.0.1:8000/api/admin/audit-logs
curl http://127.0.0.1:8000/api/admin/usage
# 也支持 period（today/7d）、module、provider、model_id、template_id、user_id、status 过滤
curl 'http://127.0.0.1:8000/api/admin/usage?provider=doubao&model_id=doubao-seedream'
# usage 返回 calls、successes、tokens、credits、cost
```

后台 API 的示例角色头为 `X-Admin-Role: admin`；当前是最小权限门禁，正式环境还需要接入登录和角色系统。

后台“模型配置”还支持新增自定义模型。新模型默认未启用，接口为 `POST /api/admin/models`；目前真实 HTTP 适配器支持豆包、千问和商汤，其他自定义模型会先以 Mock 模式保存。

自定义模型可以删除，但必须先停用且没有历史调用记录；内置的 SenseNova、Doubao、Qwen 模型不能删除，只能停用。

模型配置还支持“适用模块”和“优先级”：适用模块留空表示表情包、手机壁纸、电脑壁纸全部可用，也可以填写逗号分隔的模块 ID（`emoji`、`mobile_wallpaper`、`desktop_wallpaper`）；优先级数字越大，用户端模型下拉框越靠前。用户端切换模块时会重新加载对应模型，生成接口也会再次校验模块范围。

真实生图模型还支持备用模型、超时和重试：填写“备用模型 ID”后，当前模型连续失败会按“重试次数”重试，仍失败则切换到备用生图模型；单次超时可配置为 5–600 秒，重试次数为 0–3 次。失败尝试会进入用量台账但不扣积分，最终成功只扣 1 次积分，生成回执会返回实际使用模型和尝试记录。

模型配置页的“检查配置”只做本地配置校验，不会发起计费生图；接口为 `POST /api/admin/models/{id}/check`。

商汤 `SenseNova U1.5 Lite` 可在工作台填写：远端模型 ID `sensenova-u1.5-lite`、接口地址 `https://token.sensenova.cn/v1/images/generations`、密钥环境变量名（例如 `SENSENOVA_API_KEY`），并选择“商汤 HTTP”。启动进程配置对应密钥后，生成请求会按 SenseNova OpenAI 图片协议发送；有参考图时自动切换到 `/v1/images/edits`，响应中的图片 URL 或 Base64 图片会保存为 PNG。

模板审核可以启用 `SenseNova 6.8 Flash Lite` 的“商汤审核 Chat”适配器，接口地址为 `https://token.sensenova.cn/v1/chat/completions`。候选模板详情中的“AI 预审”只会保存 `pass/flag`、原因和分类供审核员参考，不会自动发布；接口为 `POST /api/admin/templates/candidates/{id}/ai-review`。

候选模板人工导入、批量导入和官方 API 同步都会写入审计日志。官方 API 连接器会记录最近一次同步状态、时间、批次、新增数和重复数；失败时会保留错误信息，方便运营人员定位来源问题。审核通过生成的正式模板会同时创建初始版本快照，后续编辑和回滚继续递增版本号。

后台连接器的“检查配置”只校验启用状态、授权状态、接口地址和密钥环境变量是否就绪，不会发起同步请求，也不会返回 API Key。你可以先填写环境变量名，再自行把对应 Key 写入项目 `.env` 或启动环境，检查通过后再执行同步。

审核队列支持勾选多个待审核候选模板后批量通过或拒绝；已处理的候选模板不能重复审核，避免重复创建正式模板。批量接口为 `POST /api/admin/templates/candidates/batch-review`。

商汤 `SenseNova 6.8 Flash Lite` 可选择“商汤审核 Chat”，接口地址填写 `https://token.sensenova.cn/v1/chat/completions`。模板审核队列中的“AI 预审”只会保存 `passed/flagged` 结果供管理员参考，不会自动发布模板；接口为 `POST /api/admin/templates/candidates/{id}/ai-review`。

模板抓取应优先接官方 API 或人工导入；未确认授权的平台不能直接做无授权页面抓取。官方 API 连接器可配置密钥环境变量名，密钥只从运行环境读取，不写入数据库。

- 当前 V1 仅使用 Python 标准库，可直接运行；接入真实供应商时再按适配器增加依赖。
- 这是唯一允许写产品代码的位置，不要写到桌面、引擎目录其他位置或别的文件夹。
- 每一轮新需求只做增量修改，不要整目录重建；人工改过的文件要保留。
- 验证：在引擎根目录运行 `blueprint verify out/<产品名>`，差距报告写入 `../verify/`。

## 项目目录

- `app.py`：Python 标准库服务与图片模型适配器
- `frontend/`：用户端与管理工作台
- `test_app.py`：核心功能测试
- `PRD.md`：当前产品需求
- `迭代记录.md`：实现记录
- `.env.example`：密钥环境变量示例（不包含真实密钥）
