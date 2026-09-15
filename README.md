# RS(255, 223) 声学帧译码服务

深海温盐仪回收帧的纯后端译码服务。接收 255 字节十六进制码字与互异 0 基擦除位置，
按 **RS(255, 223)** 执行错误 + 擦除联合译码（Berlekamp–Massey + Chien 搜索 +
Forney 公式，全部自实现，不依赖任何现成纠错库）。

## 编码约定

| 项目 | 约定 |
| --- | --- |
| 有限域 | GF(2⁸)，本原多项式 `0x11d`，本原元 α = `0x02` |
| 生成多项式 | g(x) = ∏_{j=0..31} (x − α^j)，即根固定为 α⁰…α³¹ |
| 字节序 | 字节 `c[i]` 是码字多项式中 x^(254−i) 的系数 |
| 系统码载荷 | 码字前 223 字节，后 32 字节为校验 |
| 综合值 | S_j = r(α^j)，j = 0…31（r 为接收多项式） |
| 译码半径 | 仅当存在满足 **2e + s ≤ 32** 的唯一码字时返回成功（e 未知错误数，s 擦除数） |

最小距离为 33，因此半径内的码字若存在必唯一；译码器在应用改正后重新计算
综合值做最终校验，绝不把"校验和碰巧通过"当作成功。

## 运行（Docker Compose）

```bash
docker compose up --build api          # 默认映射宿主 8000 端口
API_PORT=9000 docker compose up --build api   # 用 API_PORT 覆盖宿主端口
```

一次性验收服务（等待 API 健康后跑完整验收链路并退出）：

```bash
docker compose run --rm verify
```

## API

### `POST /decode`

请求体：

```json
{
  "codeword": "<510 个十六进制字符 = 255 字节>",
  "erasures": [3, 77]
}
```

`erasures` 为互异的 0 基字节位置，可省略（默认无擦除）。

**200 — 译码成功**（存在满足 2e+s≤32 的唯一码字）：

```json
{
  "status": "ok",
  "payload": "<223 字节载荷，hex>",
  "corrected_codeword": "<修复后的 255 字节码字，hex>",
  "corrected_positions": [3, 77, 200]
}
```

`corrected_positions` 为译码器实际改动的字节位置，升序；合法零综合帧
（包括多报了擦除位置的情况）返回空列表，不会虚报改正。

**422 — 不可纠正**（不存在满足半径的唯一码字，如 17 个未知错误或 33 个擦除）：

```json
{
  "status": "uncorrectable",
  "syndromes": ["48", "dc", "10", "... 共 32 个十六进制综合值 S_0..S_31 ..."]
}
```

综合值按 S_j = r(α^j)（j=0…31）定义，可用任意独立实现对收到的原始帧复算核对。

**400 — 请求非法**：码字不是 510 位十六进制、擦除位置越界（∉ [0,254]）或重复。

## 海试分片汇聚

一帧常被拆成多段、经重复投递乱序到达。`/assemblies/{assembly_id}` 提供进程内
汇聚会话，把 255 字节汇齐后交给上面同一个译码器。会话标识由调用方任选；会话
只存在于当前进程，重启后同标识等于全新会话。会话只有三种状态：

| 状态 | 含义 |
| --- | --- |
| `collecting` | 收集中，字节尚未汇齐或尚未提交译码 |
| `completed` | 已提交且译码完成，终态，结果不可改写 |
| `rejected` | 片段矛盾（重叠字节不一致或擦除声明冲突），终态，原子转入 |

### `PUT /assemblies/{assembly_id}/fragments`

请求体：

```json
{
  "offset": 60,
  "data": "<片段字节，hex，偶数长度>",
  "erasures": [2]
}
```

* `offset`：片段首字节在整帧中的 0 基位置，`[0,254]`；
* `data`：片段十六进制数据，`offset + len(data) ≤ 255`；
* `erasures`：**片内** 0 基擦除位置（上例表示帧位置 62），互异且落在片长内，
  可省略；服务端按 `offset` 翻译成帧位置并跨片段去重。

片段可乱序、可重叠（重叠字节必须一致）、可无限次同内容重试，重试只回当前进度。
收集中与汇齐（`complete: true`）但尚未 `POST .../decode` 时返回 **200**：

```json
{
  "status": "collecting",
  "assembly_id": "storm-7",
  "received_bytes": 110,
  "total_bytes": 255,
  "complete": false,
  "missing_ranges": [{"start": 0, "end": 59}, {"start": 111, "end": 254}],
  "erasures": [62],
  "decode": null
}
```

重叠字节不一致，或在已可靠送达的位置上声明擦除（反之亦然）时，整个会话**原子**
转为 `rejected`（当前片段一字节都不会落库），返回 **409** 及规范化（合并相邻点、
升序、闭区间）冲突区间；此后任何片段或提交都原样重放该 409：

```json
{
  "status": "rejected",
  "assembly_id": "storm-7",
  "conflict_ranges": [{"start": 5, "end": 9}]
}
```

重叠字节不一致（**包括**两片段都把该位置声明为擦除、但携带的填充字节不同——
同一帧的两份拷贝不应在任何位置上值不一致，且判定与到达顺序无关），或在已可靠
送达的位置上声明擦除（反之亦然）时，整个会话**原子**转为 `rejected`（当前片段
一字节都不会落库），返回 **409** 及规范化（合并相邻点、升序、闭区间）冲突区间；
此后任何片段或提交都原样重放该 409：

```json
{
  "status": "rejected",
  "assembly_id": "storm-7",
  "conflict_ranges": [{"start": 5, "end": 9}]
}
```

两边携带相同字节、且都声明同一位置为擦除时不算矛盾（只是重复投递）；对同一位置
重复声明擦除只做去重。请求体本身非法（非 hex、越界、片内擦除越界或重复等）
返回 **400**，不改变会话。

### `POST /assemblies/{assembly_id}/decode`

* 尚有缺口：**409**，信封同 200 进度体，`missing_ranges` 为规范化缺失区间，
  `decode` 为 `null`，不调用译码器，会话保持 `collecting`；
* 已汇齐：把码字与去重后的擦除位置交给现有译码器，**首次结果永久缓存**——
  成功 **200**、不可纠正 **422**，信封 `status` 为 `completed`，内嵌的
  `decode` 字段与 `POST /decode` 的 200/422 响应体逐字段相同；
* 之后的重复提交、晚到片段（含与终态矛盾的片段）一律重放同一终态：同状态码、
  同响应体，译码器只执行一次；
* 已拒绝会话上提交：重放 **409** 冲突区间。

```json
{
  "status": "completed",
  "assembly_id": "storm-7",
  "received_bytes": 255,
  "total_bytes": 255,
  "complete": true,
  "missing_ranges": [],
  "erasures": [3, 62, 200],
  "decode": {
    "status": "ok",
    "payload": "…223 字节…",
    "corrected_codeword": "…255 字节…",
    "corrected_positions": [3, 62, 200]
  }
}
```

### 并发与隔离

同一会话的追加与提交在会话锁内线性化：并发提交与"最后一片竞争"只有一个请求
能触发译码，终态唯一且可复现。不同会话使用各自的锁，互不阻塞。

### `GET /health`

返回 `{"status": "healthy"}`，供 Compose 健康检查使用。

## 请求示例

下面是一帧真实可复现的报文（载荷由 `app.rs.encoder` 生成，位置 3、77、200
被损坏，其中 77 同时声明为擦除）：

```bash
curl -s -X POST http://localhost:8000/decode \
  -H 'Content-Type: application/json' \
  -d '{
    "codeword": "a54dca4d2530bb1d6d132cded6237b2ed91e3f721fcb1971174494d6493c9d5c3460be31201e69fedaa0eee8b9997f5c7c2999fdafe593253cd654af4dfad71427a0aeb3fee9232f8af2211f9ee591c5b10becb5563bfc1e6f93427ecbc8fe2955e5cd8e46dc8ed4b7c2764d2a5a4d767706f85d8690024ad6bda3401be9c8cbccc935f6cd1f61226ae15338ae1a34004d33ba0d246ac04c81b1baf23e3bf9eef5f79f2b4934af87f5520b69b94b0d982e85bb55b672a872637acd7466fcb60e0e8ff18463b0e4b22329703474f064ac68f700f5b02b3dc666f45bdeaa2ccad0a0a4b01315095edc2e735b6a476c65843e0014af27bd11945c6b274c5af392",
    "erasures": [77]
  }'
```

响应（200）：

```json
{
  "status": "ok",
  "payload": "a54dca182530bb1d6d132cded6237b2ed91e3f721fcb1971174494d6493c9d5c3460be31201e69fedaa0eee8b9997f5c7c2999fdafe593253cd654af4dfad71427a0aeb3fee9232f8af2211f9ee491c5b10becb5563bfc1e6f93427ecbc8fe2955e5cd8e46dc8ed4b7c2764d2a5a4d767706f85d8690024ad6bda3401be9c8cbccc935f6cd1f61226ae15338ae1a34004d33ba0d246ac04c81b1baf23e3bf9eef5f79f2b4934af87f5520b69b94b0d982e85bb55b672a872637acd7466fcb60e0e8ff18463b0e4b2ba29703474f064ac68f700f5b02b3dc666f45bdeaa2cca",
  "corrected_codeword": "a54dca182530bb1d6d132cded6237b2ed91e3f721fcb1971174494d6493c9d5c3460be31201e69fedaa0eee8b9997f5c7c2999fdafe593253cd654af4dfad71427a0aeb3fee9232f8af2211f9ee491c5b10becb5563bfc1e6f93427ecbc8fe2955e5cd8e46dc8ed4b7c2764d2a5a4d767706f85d8690024ad6bda3401be9c8cbccc935f6cd1f61226ae15338ae1a34004d33ba0d246ac04c81b1baf23e3bf9eef5f79f2b4934af87f5520b69b94b0d982e85bb55b672a872637acd7466fcb60e0e8ff18463b0e4b2ba29703474f064ac68f700f5b02b3dc666f45bdeaa2ccad0a0a4b01315095edc2e735b6a476c65843e0014af27bd11945c6b274c5af392",
  "corrected_positions": [3, 77, 200]
}
```

不可纠正示例（17 个未知错误）返回 422 及 32 个可复算综合值：

```bash
curl -s -X POST http://localhost:8000/decode \
  -H 'Content-Type: application/json' \
  -d '{"codeword": "a54dca182530bb906d132cded6237b2ed91e3f721f301971174494d6493c9d5c1c60be31201ee4fe73a0ee18b9997f5c7c2999fdafe593253cd654af4dfad71427a0aeb3fee9232f8af2211f9ee491c5b10becb5563bfc1e6f93427ecbc8fe2955e5cd8e46dc13d4b7c2764d2a5a4d767706f85d8690244ed6bda3401be9c8cbccc935f6cd1f61226ae15338ae1a34004d33ba0d246ac06e81b1baf23e3bf9eef5f79f2b4934af7ef5520b69b94b0d982e85bb55b672a8726300cd7466fc620e0e8ff18463b0e4b2ba29703474f0a9ac68f700f5b02b3dc666f45bdeaa2ccad0a0a4b01315095eda2e735b6a476c65843e008daf27bd11945c6b274c5af3c8", "erasures": []}'
# HTTP 422
# {"status":"uncorrectable","syndromes":["48","dc","10","e3","81","24","da","3c",
#  "23","11","36","98","90","0c","1c","c1","b5","34","58","d7","d6","e5","42","e4",
#  "46","83","c7","0b","64","c4","31","51"]}
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

覆盖：GF(256) 域公理与 α 本原性、综合值定义与可加性、编码器系统性与生成
多项式根、译码半径边界（16 错误 / 10 擦除+11 错误可恢复，17 错误 / 33 擦除
稳定拒绝）、随机往返，以及完整 HTTP 接口链路（200/400/422 语义）、分片汇聚
（乱序还原、幂等重试、规范化缺口/冲突区间、原子拒绝、终态不可改写、并发
线性化与最后一片竞争）。

一次性验收 `python -m verify.acceptance`（或 `docker compose run --rm verify`）
在原译码链路之外，还会对在线 API 跑完整的分片汇聚场景，含 12 线程的最后一片
竞争。

## 结构

```
app/
  main.py          FastAPI 入口，/decode、/health 与分片汇聚两接口
  schemas.py       Pydantic 请求/响应模型（400 校验）
  assembly.py      汇聚会话领域对象与进程内并发存储（会话锁 + 创建门）
  rs/
    gf.py          GF(256) 指数/对数表与四则运算（0x11d, α=0x02）
    poly.py        域上多项式：乘、求值、形式导数
    encoder.py     系统码编码器（测试与验收用）
    decoder.py     错误+擦除联合译码：BM、Chien、Forney
verify/
  acceptance.py    一次性验收服务（compose 的 verify）
tests/             pytest：有限域、综合计算、接口链路、汇聚与并发
```
