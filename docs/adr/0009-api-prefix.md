# ADR 0009 · API 路径统一加 `/api` 前缀

- 状态：已采纳
- 日期：2026-09-30
- 关联：`docs/contracts/openapi.yaml`、ADR 0008

## 背景

ADR 0008 决定引入单页应用后，前端与 API 需要同源部署：生产环境由 FastAPI 把 `frontend/dist` 挂在根路径，前端路由与 API 路径共用同一个域名与端口。

在引入前端之前，API 路径是 `/analyze`、`/runs/{run_id}`、`/jobs/{job_id}` 这样的扁平结构，没有前缀冲突问题。引入前端路由后就出现了直接碰撞：

```
前端路由  /runs/abc123        （查看某次运行的页面）
API 路径  /runs/{run_id}      （读取某次运行的 JSON）
```

同一路径既要返回 HTML 又要返回 JSON，只能靠 `Accept` 头或 User-Agent 猜，属于典型的耦合设计。

## 备选方案

| 方案 | 问题 |
| --- | --- |
| 前端与 API 分端口部署 | 需要处理 CORS、跨域 Cookie、两个服务同时启动；与"`docker compose up` 一条命令起全栈"的目标冲突 |
| 依赖 `Accept` 头区分 HTML 与 JSON | 隐式魔法，调试困难；任何代理或缓存都可能破坏判断 |
| 前端路由加前缀（如 `/app/runs/abc`） | 把复杂度转移到前端，用户看到的地址更丑，且新增前端路由时仍要小心避让 API 路径 |
| **API 统一加 `/api` 前缀** | 一次性破坏性改动，但语义清晰、边界干净 |

## 决策

**所有 API 路径统一加 `/api` 前缀**：

```
/api/health
/api/analyze
/api/jobs/{job_id}
/api/runs/{run_id}
/api/runs/{run_id}/report
```

根路径 `/` 及其余非 `/api` 路径全部交给前端路由处理。原来的 `GET /`（返回演示页）从 API 契约中移除——它属于前端职责，不再是 API。

## 后果

**正面**

- 前端路由与 API 路径彻底分离，互不干扰；
- 反向代理与网关只需一条 `/api` 规则，部署心智简单；
- 前端生成客户端时，`baseUrl` 固定为 `/api`，开发与生产一致。

**负面**

- 破坏性变更：所有已写的接口路径、示例与文档都要改（当前尚无实现代码，代价仅限于文档）；
- 前端开发时 Vite 需要配置 `/api` 代理到 FastAPI，多一行配置。

**后续**

- 该前缀写进 `docs/contracts/openapi.yaml` 的 `servers` 与全部路径；
- 前端 `VITE_API_BASE_URL` 默认 `/api`，开发态由 Vite 代理，生产态同源直连。