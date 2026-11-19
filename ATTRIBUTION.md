# 相邻封账的净差归因

接班人已有两个封账号，要核对后版相对紧邻前版的净报送额由哪些原批产生。零额也可能包含真实变化。归因只在本地工作账领取。

## 入口

- `audit.attribution.enable_attribution(local)` 在现有工作账启用归因，兼容已有 V1 数据的库。以首次成功启用为界，再调用维持原界。后续仍用 `intake.batches.apply` 接纳和 `audit.purge.purge` 清理。
- `audit.lookup.explain_delta(local, earlier_seal_id, later_seal_id)` 接受两个封账号。未知号、无效号、跨日、倒序、同号或跳版均抛 `meter_core.InputError`。合法配对但后版尚无本地确认时，返回 `{"state": "pending", "receipt": None, "changes": None}`。
- 后版已确认且证据完整时，返回 `{"state": "confirmed", "receipt": receipt, "changes": changes}`。`receipt` 是后版原 `Receipt`，`changes` 是有序列表。每项为含 `batch_id`、`station`、`name`、`position`、`before`、`after`、`delta` 的字典。`position` 是批内原始一基位置。整升值和差额沿用 V1 的 Python 整数口径。缺证或逐笔合计与回执不符时抛 `meter_core.EvidenceError`，错误说明应能区分缺证与核对不符，不返回部分结果。

## 归因范围

只取早版来源水位之后、后版来源水位以内的同日批，按批次顺位和批内原位置排列。首录的 `before` 为 `None`，差额从零起算，零升首录也保留。同值勘误省略，原位置保持。保留同一站行的每笔真实变化，包括相互抵消的变化。之后的接纳、封账和报送不改变已定区间。

对首次启用前的批，完整旧前缀指从该日第 1 批到所查旧水位所需的原件在首次启用时仍完整。这样的旧批和启用后成功接纳的批，在到期清理和重开后仍须可核。首次启用时同日旧前缀已遭清理的，涉及该旧段的非空区间报缺，即使还留有后续原件。同日从首次启用的输入水位起的区间仍可核。尚未启用的已确认非空区间也报缺。两版水位相同时无输入变化，后版若已确认，核对原回执为零即返回完整空清单，包括其他旧区间已缺证的日。

维持原清理资格和删除效果，未确认的定稿仍可覆盖原件。原样重投不新增变化。启用、接纳或清理失败不推进覆盖或遗留部分证据，原接纳、定稿、报送与核账接口继续可用。

## 样例与运行

`python3 -B attribution_examples.py` 展示 V1 原件清理后的账面情况。脚本的 `prepare_legacy(local, external, peak)` 可用于构造尚未启用归因的旧账，它先接纳 a=2、b=3 并确认 `first`，再接纳 c1 将 b 改为 `peak`、c2 将 b 改回 3，并确认 `second`。脚本运行时会清除这三批原件，重新打开后仍可通过 V1 读取原回执。

| 首次启用前的状态与输入 | 启用后领取 `first` 到 `second` 的预期 |
| --- | --- |
| 原件完整，`peak=10`，启用后清理并重开 | c1 的 b 从 3 到 10、差额 +7，c2 的 b 从 10 到 3、差额 −7。原回执为 0。 |
| 原件完整，`peak=8`，启用后清理并重开 | 同一来源顺位分别交付 +5、−5。原回执为 0。 |
| 原件完整，`peak=3` | 原回执为 0，`changes` 为空列表。 |
| 首次启用前原件已清，`peak=10` | 旧区间报缺。若启用后把 b 从 3 改为 4，封账并确认 `third`，从 `second` 到 `third` 仍可核出 +1。 |

提交失败可使用 `tests/support.py` 的 `rejected_commit(local)` 核验。保留原测试与演示。沿 `.pointwise-runtime.json` 接通新旧验证和演示，清单仍为版本 1 的 CLI 清单，`setup` 是参数数组的列表，`verify` 与 `demo` 是参数数组。验证失败须以非零状态退出。演示须显示清理重开后实际读取的原回执及两笔归因。
