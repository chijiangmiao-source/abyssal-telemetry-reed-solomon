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
稳定拒绝）、随机往返，以及完整 HTTP 接口链路（200/400/422 语义）。

## 结构

```
app/
  main.py          FastAPI 入口，/decode 与 /health
  schemas.py       Pydantic 请求/响应模型（400 校验）
  rs/
    gf.py          GF(256) 指数/对数表与四则运算（0x11d, α=0x02）
    poly.py        域上多项式：乘、求值、形式导数
    encoder.py     系统码编码器（测试与验收用）
    decoder.py     错误+擦除联合译码：BM、Chien、Forney
verify/
  acceptance.py    一次性验收服务（compose 的 verify）
tests/             pytest：有限域、综合计算、接口链路
```
