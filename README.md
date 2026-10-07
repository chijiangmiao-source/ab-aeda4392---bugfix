# 宏卫生展开复核台（Macro Hygiene Review）

航电规程团队在将**可复用参数片段**（`syntax-rules` 宏）编入飞控脚本前，
用本服务复核**宏展开不会让模板临时变量捕获调用点的同名控制量**。

## 能力

- 粘贴**受限 S 表达式模块**：至多 **12 个宏**、每宏至多 **8 条规则**；
  支持 `lambda`、`let`、过程调用、`syntax-rules`（重复 `...` 仅允许一层）。
- 对含**嵌套宏调用且调用点 `x` 与模板临时 `x` 同名**的合法模块，页面展示：
  - 规范化展开结果（每个标识符标注绑定身份 `⁽Bn⁾` / 全局 `⁽Fn⁾`）；
  - 每一步命中的**规则编号、规则文本**与**调用位置（行/列，可点击回溯源码）**；
  - 明确判定同名二者属于**不同绑定身份**（模板引入作用域 vs 调用点）。
- 卫生规则（Dybvig–Hieb marks / 集合作用域算法）：
  - **literal 按词法绑定比较，而非拼写**（局部 `if` 遮蔽不会匹配 literal `if`）；
  - **模式变量保留调用点绑定**（引入/使用作用域消去后身份不变）；
  - **模板新绑定获得稳定的新作用域**（每个 `define-syntax` 唯一引入作用域，
    每次调用唯一使用作用域）。
- 失败场景定位**首个相关源片段**（行列 + 代码片段指示）并给出
  **稳定证据码**（对同一模块两次复核一致），同时清除旧结论：
  规则无匹配、重复变量长度不一致、未绑定 literal、递归超限（>64 层）、
  **累计资源预算超限**（`expansion-budget`）、语法不完整。
- **累计资源预算**（单次复核独立，可经环境变量配置）：
  - `MACRO_REVIEW_MAX_EXPANSIONS`（默认 **2000**）：累计宏展开次数上限；
  - `MACRO_REVIEW_MAX_OUTPUT_CHARS`（默认 **200000**）：累计展开输出字符上限。
  复制型模板即便嵌套仅 16 层（远未触及 64 层单路径深度限制）也会产生
  2¹⁶−1 次展开；预算耗尽时在构造大量步骤证据前受控中止，错误关联
  **原始调用位置**，不返回任何部分结论或步骤记录。
- 递归/预算超限**不阻塞后续合法模块复核**：每次 `review()` 使用全新状态。

## 目录

| 路径 | 说明 |
| --- | --- |
| `app/syntax.py` | 带源码 span 与作用域集合的 S 表达式读取器 |
| `app/engine.py` | 卫生展开器：规则编译/匹配/模板实例化、词法解析、身份标注 |
| `app/server.py` | 标准库 HTTP 服务：页面 + `POST /api/review` + `/health` |
| `app/static/index.html` | 规程复核页面（无框架、无构建步骤） |
| `tests/test_engine.py` | 29 项卫生/literal/重复/错误/隔离/限额/累计预算代码测试 |
| `verify.py` | **一次性验收服务**：代码测试 + 页面构建 + API/HTTP 冒烟，按退出码报告 |
| `Dockerfile`, `docker-compose.yml` | 容器化与 Compose 编排 |

## 本地直接运行（无需 Docker）

```bash
python3 -m app.server            # 默认 0.0.0.0:8080，可用 PORT 环境变量改端口
python3 verify.py                # 一次性验收（自动在临时端口起服务，冒烟后退出）
python3 -m unittest discover -s tests -v
```

## Compose

```bash
# 启动规程页面与健康服务（宿主端口可配置，默认 8080）
HOST_PORT=9090 docker compose up --build web
#   页面:  http://localhost:9090/
#   健康:  http://localhost:9090/health  -> {"status":"ok",...}

# 一次性验收服务：在 Compose 内等待 web 健康后完成
#   代码测试 -> 页面构建 -> 页面/健康地址 API/HTTP 冒烟，随后退出并以退出码报告
docker compose up --build verify
docker inspect --format '{{.State.ExitCode}}' \
  $(docker compose ps -q verify)   # 0 = 验收通过
```

`verify` 服务在 Compose 内通过 `BASE_URL=http://web:8080` 访问 `web`；
脱离 Compose 直接运行 `python3 verify.py` 时会自启临时 HTTP 服务完成同样检查。

## HTTP API

```
GET  /health            -> {"status": "ok", "service": "macro-review"}
GET  /                  -> 规程复核页面
GET  /api/sample        -> {"source": "…示例模块…"}
POST /api/review        <- {"source": "(define-syntax …) …"}
```

`/api/review` 始终返回 200 与结构化 JSON：

- 成功：`ok:true`、`steps[]`（命中规则与调用位置）、`normalized`、
  `identities[]`、`hygieneChecks[]`，并附 `expansions`/`outputChars`/`budget`；
- 失败：`ok:false`、`error{kind,message,line,column,snippet,evidence}`
  （预算错误另附 `budget`/`limit`/`used`），
  且 `steps`/`normalized` 为空（旧结论清除）。

## 资源预算配置

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| `MACRO_REVIEW_MAX_EXPANSIONS` | 2000 | 单次复核累计宏展开次数 |
| `MACRO_REVIEW_MAX_OUTPUT_CHARS` | 200000 | 单次复核累计展开输出字符 |

单条嵌套路径的 64 层深度限制仍然保留，但它只约束单路径递归；
真正限制复制型模板总体资源占用的是上述累计预算。
