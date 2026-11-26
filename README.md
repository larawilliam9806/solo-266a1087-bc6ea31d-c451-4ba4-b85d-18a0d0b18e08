# Kubernetes 故障恢复演练台

一个使用 Flask 的本地演练页面：上传最多 **8 台节点**的 JSON 快照，选择一台故障节点，试算其最多 **8 个在线 Pod** 如何迁入健康节点。求解器会给出完整落点和需要停用的可中断批作业 Pod，并保证停用数量最少。

> 本工具只做离线试算，不会连接 Kubernetes，也不会真正停用或迁移任何 Pod。

## 功能与约束

- JSON 文件上传或文本粘贴，支持字段别名和 Kubernetes 风格内存字符串。
- 页面展示节点可用区、可分配内存，以及每个 Pod 的位置、应用、类型、内存请求、卷可用区和可中断标记。
- 只迁移故障节点上的 `online` Pod；故障节点上的批作业 Pod 不纳入迁移。
- 健康节点上的 Pod 默认原地保留。
- 只有 `type: "batch"` 且 `interruptible: true` 的 Pod 可被停用。
- 落点必须同时满足：
  - 停用选中的批作业后，Pod 内存请求总和不超过节点可分配内存；
  - Pod 有卷可用区时，落点节点必须位于同一可用区；
  - 同一 `app` 的副本不能位于同一节点；
  - 所有待迁移在线 Pod 都必须有落点；不输出部分方案。
- 若存在完整方案，目标函数是 **停用 Pod 数量最少**。
- 无完整方案时明确提示“无法恢复”，并给出常见阻塞原因。

## Docker Compose 启动

```bash
docker compose up --build
```

启动后访问：

- 页面：<http://localhost:5000>
- 健康检查：<http://localhost:5000/healthz>
- 示例快照：<http://localhost:5000/examples/snapshot.json>

停止：

```bash
docker compose down
```

## 本地直接运行

需要 Python 3.11+：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python wsgi.py
```

也可以使用：

```bash
flask --app wsgi run --debug
```

## JSON 快照格式

最小规范格式如下，内存数字默认使用 MiB：

```json
{
  "nodes": [
    {
      "name": "node-a",
      "zone": "az-a",
      "allocatableMemoryMiB": 12288
    },
    {
      "name": "node-b",
      "zone": "az-b",
      "allocatableMemoryMiB": 8192
    }
  ],
  "pods": [
    {
      "name": "order-0",
      "node": "node-a",
      "app": "order",
      "type": "online",
      "memoryRequestMiB": 2048,
      "volumeZone": "az-a",
      "interruptible": false
    },
    {
      "name": "batch-cleanup-1",
      "node": "node-b",
      "app": "cleanup",
      "type": "batch",
      "memoryRequestMiB": 4096,
      "volumeZone": null,
      "interruptible": true
    }
  ]
}
```

仓库自带可恢复与不可恢复两个示例，可在首页点击按钮载入：[`examples/snapshot.json`](examples/snapshot.json)、[`examples/unrecoverable.json`](examples/unrecoverable.json)。

### 兼容字段

除规范字段外，解析器还支持常见别名，例如：

- 节点：`nodeName`、`availabilityZone`、`az`、`status.allocatable.memory`
- Pod：`nodeName`、`appName`、`workloadType`、`memoryRequest`
- 内存：`resources.requests.memory`、`512Mi`、`2Gi`、`1000M`
- 卷：`volumeAz`、`volumeAvailabilityZone`、`volume.zone`
- 中断：`canEvict`、`evictable`、`preemptible`
- 中文字段：`可用区`、`可分配内存`、`位置`、`应用`、`类型`、`内存请求`、`卷可用区`、`能否中断`

## JSON API

```bash
curl -sS http://localhost:5000/api/recover \
  -H 'Content-Type: application/json' \
  -d '{
    "failed_node": "k8s-node-1",
    "snapshot": {
      "nodes": [],
      "pods": []
    }
  }'
```

也可以直接把快照和 `failed_node` 放在同一个 JSON 对象中提交。

成功响应的关键字段：

```json
{
  "feasible": true,
  "success": true,
  "minimumStoppedPods": 1,
  "placements": [
    {"pod": "search-7", "node": "k8s-node-2", "zone": "az-a"}
  ],
  "stops": [
    {"pod": "batch-reindex", "node": "k8s-node-2", "reason": "..."}
  ],
  "nodeReports": []
}
```

无法恢复时 HTTP 200，响应中 `feasible: false`，并包含 `blockers`；快照或参数不合法时返回 HTTP 400。

## 求解思路

由于故障节点最多有 8 个在线 Pod，可以用位掩码表示 Pod 子集（最多 `2^8 = 256`）。

1. 对每台健康节点，枚举可能整体放入该节点的 Pod 子集。
2. 对每个子集：
   - 校验子集内部没有相同应用；
   - 校验每个卷可用区；
   - 处理同应用现存 Pod：不可停用则该子集无效，可停用则列入必要停用集合；
   - 对内存缺口，从其余可中断批作业中按内存请求从大到小选择最少数量的 Pod。
3. 用位掩码动态规划逐节点合并子集，保证每个在线 Pod 恰好分配一次。
4. 在完整掩码的方案中选择停用总数最小的方案。

该方法是精确枚举，不是贪心近似。测试中还使用随机小规模实例与“全部落点组合 × 全部停用子集”的暴力结果进行了一致性对比。

## 测试

```bash
python -m unittest discover -s tests -v
```

或：

```bash
pip install pytest
pytest -q
```

## 目录结构

```text
.
├── app/
│   ├── __init__.py       # Flask factory
│   ├── memory.py         # MiB / Kubernetes memory quantity parsing
│   ├── normalize.py      # snapshot validation and aliases
│   ├── routes.py         # HTML + JSON routes
│   └── solver.py         # exact bitmask DP planner
├── examples/snapshot.json
├── static/
├── templates/
├── tests/
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
