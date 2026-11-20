# 统计区改划 · 新旧区人数精确换算服务

区划调整后，历年按旧区公布的人数需要换算到新区。本服务用两个独立的
FastAPI 实例完成这件事，全程使用有理数精确运算（`fractions.Fraction`），
不使用浮点数。

## 大规模清单与 192 MiB 内存限制

需求规模可达 60 万个街块、新旧区各 240 个、每区至多 5000 个街块，而进程
地址空间限制为 192 MiB。朴素实现会把整份 JSON 读成 Python 对象树，并构造
60 万行的稠密 `Fraction` 矩阵，收件或构包阶段就会耗尽内存。本服务从两端
压缩内存：

- **收件（流式解析）**：`/build` 不再让 FastAPI/Pydantic 一次性加载请求体，
  而是用 `app/streaming.py` 中手写的增量 JSON 解析器逐字符读取。结构标点
  （`{ } [ ] , :`）逐字符处理，字符串按 JSON 转义规则手工扫描，任意 token
  被网络分包截断都能在下一个 chunk 续上；每个街块编号读完即入账，不保留
  任何按区排列的街块清单。
- **存储（位掩码）**：每个街块只在一个 `dict` 里对应一个 Python 整数位掩码，
  低 240 位记录旧区归属、高位记录新区归属。
- **构包（按旧区签名聚合 + 稀疏消元）**：旧区归属完全相同的街块对所有线性
  代数计算等价，按“旧区签名”聚合成组（最多约 2.9 万组），用稀疏字典做前向
  消元，最后只对一个至多 240×480 的小矩阵做一次稠密 RREF。

实测在硬 192 MiB `RLIMIT_AS` 下处理完整 60 万街块清单，峰值 RSS 约 124 MiB，
基准脚本见 `bench/bench_mem.py`。

### 整次失败与“读到请求末尾”

解析器始终消费整个请求体；语义错误与 JSON 语法错误先记录、在到达流末尾后
才统一抛出，因此**末段**出现的同一区内重复街块编号、JSON 损坏或截断都会让
整次请求失败并返回错误原因：

- JSON 语法/UTF-8/截断/尾随垃圾 → HTTP 400，`error="invalid_json"`；
- 语义约束（重复编号、重复区号、区列表为空、超过 240 个区等）→ HTTP 422，
  `error="invalid_request"`。

### 孪生街块规则

若两个街块的**旧区归属完全相同**，但其中只有一个归入某个新区，则该新区的
覆盖关系无法用旧区人数精确表达（同一个旧区观测无法区分这两个街块），该新区
在交接包中标记为 `"derivable": false`，换算时进入 `missing`，原因是
`not_expressible_from_old_districts`。若新区对这对街块一视同仁（同时包含或
同时不包含），则不受影响，仍按精确分数系数推导。

## 数学模型

设街块人数为未知量 x，0/1 关联矩阵：

- `A`（街块 × 旧区）：旧区人数 `p = A·x`
- `B`（街块 × 新区）：新区人数 `q = B·x`
- 同一街块可同时属于多个区（矩阵列允许重叠）

由此得到两类精确代数关系：

1. **旧区之间的等式**：`ker(A)` 中的每个向量 k 满足 `k·p = 0`；
2. **新区对旧区的表达式**：对每个新区列 bⱼ 解 `A·c = bⱼ`，
   有解则 `qⱼ = c·p`，系数为精确分数（如 `1/2`、`-3`）。

## 两个实例

| 实例 | 端口 | 接口 | 职责 |
|---|---|---|---|
| package-builder | 8001 | `POST /build`，文档 `/docs` | 输入新旧区号及各自覆盖街块，输出 JSON 交接包 |
| converter | 8002 | `POST /convert`，文档 `/docs` | 输入交接包与本次有数的旧区人数，输出新区人数 |

### 交接包内容（不含任何街块编号或逐块归属）

```json
{
  "schema_version": 1,
  "old_districts": ["O1", "O2"],
  "new_districts": ["N1"],
  "old_relations": [{"O1": 1, "O2": -1}],
  "new_district_relations": [
    {"code": "N1", "derivable": true, "coefficients": {"O1": "1/2", "O2": "1/2"}}
  ]
}
```

- `old_relations`：旧区人数必须满足的精确线性等式（加权和为 0），
  整数系数，已约分为本原向量；
- `new_district_relations`：每个新区一个条目，`derivable=false` 表示
  覆盖关系无法用旧区精确表达，`coefficients` 为 `"p/q"` 形式的精确分数
  （整数写作 `"2"`）。

### 换算规则（converter）

- **可算范围只按本次提交的旧区数据判定**：仅当新区表达式能（借助交接包
  内的旧区关系）改写为只含本次有数旧区时才计算，其余新区列入 `missing`，
  并注明 `not_expressible_from_old_districts` 或
  `insufficient_known_old_districts`；
- 本次有数旧区所支撑的每条旧区等式都会实际校验，结果放在
  `checked_relations`；等式涉及无数据旧区时自动跳过；
- 旧区人数必须是非负整数（请求体 schema 强制）；
- **整次失败**（HTTP 400，只返回错误体）的情形：
  - 旧区人数违背覆盖关系推出的等式；
  - 任一新区换算结果为负数；
  - 任一新区换算结果不是整数（分数系数导致的半整数等）。

成功响应示例：

```json
{
  "available_old_districts": ["O1", "O2"],
  "checked_relations": [],
  "computed": [
    {"code": "N1", "status": "computed", "population": 8,
     "coefficients": {"O1": "1/2", "O2": "1/2"},
     "expression": "1/2*O1 + 1/2*O2", "missing_reason": null}
  ],
  "missing": [],
  "results": [ /* computed 与 missing 的完整列表，顺序与新区一致 */ ]
}
```

## Docker Compose 启动

```bash
docker compose up --build
```

- 交接包服务（含 Swagger 交互文档）：<http://localhost:8001/docs>
- 换算服务（含 Swagger 交互文档）：<http://localhost:8002/docs>
- 健康检查：`GET /health`
- 两个容器都配置了 `mem_limit: 192m`（含 swap 上限），与进程地址空间约束一致。

在 `http://localhost:8001/docs` 的 `POST /build` 中仍按原 JSON 形态提交
（`old_districts` / `new_districts`，结构未变），Swagger 提供请求体编辑器；
服务端在内部改为流式解析。生成的 v1 交接包以及任何既有 v1 包，都可直接在
`http://localhost:8002/docs` 的 `POST /convert` 中使用。

## 本地运行与测试

```bash
pip install -r requirements.txt
pip install pytest httpx
uvicorn app.builder_app:app --port 8001
uvicorn app.converter_app:app --port 8002
pytest -q
```

## 典型调用流程

1. 在 `http://localhost:8001/docs` 的 `POST /build` 提交新旧区及街块覆盖，
   保存返回的交接包 JSON；
2. 在 `http://localhost:8002/docs` 的 `POST /convert` 提交该交接包与
   当期已有旧区人数（非负整数），当次得到可精确推出的新区人数、
   带分数系数的算式，以及缺资料新区列表。

## 目录结构

```
app/
  rationals.py      # 精确有理线性代数（RREF、零空间、线性方程组）
  expressions.py    # 分数与算式格式化
  models.py         # Pydantic 请求/响应模型与校验
  builder_core.py   # 位掩码累加、签名聚合、稀疏消元与构包（内存受限核心）
  streaming.py      # 手写增量 JSON 解析器（逐字符、可跨 chunk、读到 EOF）
  engine.py         # 换算纯领域逻辑（构包入口委托给 builder_core）
  builder_app.py    # 实例 1：流式 /build
  converter_app.py  # 实例 2：/convert
tests/              # 端到端与流式/内存/孪生规则测试（pytest）
bench/              # 192 MiB 全规模内存基准与交叉校验脚本（非交付包内容）
Dockerfile
docker-compose.yml  # 两个服务，均带 192 MiB 内存硬限制
```
