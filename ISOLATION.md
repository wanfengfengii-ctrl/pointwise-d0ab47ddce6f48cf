# 固定批次的质量隔离

README.md 中的来源接纳、计划语法、遍历顺序、函数和严格运行仍适用。隔离按每次运行选择，来源批次与计划修订在运行创建前固定。同名计划后来登记新修订，旧运行仍按原绑定读取。

## 调用与读取

`Warehouse.run_isolated(batch_id, plan_revision, function_context=None)` 与 `run` 接受同样的参数，每次创建独立运行，返回 `RunReceipt(run_id, row_count)`。`row_count` 只计存续明细。未提供函数上下文时，每次使用新的上下文。

`Warehouse.read_isolated_result(run_id)` 返回具有 `run_id`、`batch_id`、`plan_revision`、`row_count`、`rows` 和 `rejections` 属性的结果。`rows` 是有序 `OutputRow` 元组，沿用原明细形状和坐标，从 0 连续编号。`rejections` 是按发现顺序排列的元组，每项为 `{"owner": 坐标字典, "error": 错误摘要}`。坐标字典沿用 `encode_coordinate` 的 `batch_id`、`record_index` 和 `elements`，进入明细层前的来源坐标具有空 `elements`。错误摘要沿用 `EvaluationError.to_data()`，其中 `location` 仍指原始失败表达式和首次需求坐标。两份数据可分别为空。

两个读取入口对未知运行、未成功运行或另一模式的运行抛出 `ResultUnavailable`。严格入口仍可按原运行号读取已有 V1 数据库中的历史成果。

## 隔离规则

仅 `FunctionFailure`、`MissingField` 和 `TypeMismatch` 是质量拒绝。出错表达式的求值环境决定拒绝拥有者。绑定表达式由绑定声明所属的出现拥有。筛选、展开和投影直接出错时，由该表达式声明所处的出现拥有。经引用传播时保留最初出错的表达式、所属出现和首次需求坐标。同一失败向外传播只产生一次拒绝。左展开的空侧出现仍用 `element_index=None`，与真实数组元素区分。

撤回拥有者范围内此前产生的明细，并停止求值该出现下尚未执行的部分。范围外仍按原深度优先顺序执行。保留已有的求值顺序、短路和函数调用效果。已发出的票号不能收回，也不能重跑求值重建存续明细。后来的祖先拒绝只改变明细资格，已发现的子级拒绝仍留在清单。允许全量内存缓冲和提交前扫描候选明细。

存续明细、拒绝清单和成功终态属于同一个原子发布。存储错误、绑定环及其他非质量异常沿用原整次失败边界。可继续使用 `warehouse.database.inject_once('run.before_commit')` 检查工作事务提交前失败。注入失败后两种入口都不可读取该次运行，先前的已发布成果继续可读。

## 样例及运行入口

`isolation_examples.py` 使用现有计划语法。根绑定和元素绑定两例都在来源 0 的元素 1 取票以后首次需求失败绑定。根绑定失败时撤回来源 0 的票号 1，跳过元素 2，来源 1 保留票号 3，输出序号为 0。元素绑定失败时只拒绝元素 1，保留票号 1、3、4。两种拒绝记录的首次需求坐标相同，拥有者坐标分别是来源 0 和该来源的元素 1。嵌套样例先发现叶级拒绝，再发现上一级元素拒绝，清单保留两项，后续同级元素仍能交付。脚本内的期望明细、拒绝和轨迹为手算值。

基线用 `python3.11 -B isolation_examples.py baseline` 核对相同输入的严格失败。交付时沿用版本 1 的 CLI 运行清单 `.pointwise-runtime.json`，`setup` 是准备命令的 argv 列表，无准备步骤时为空。将 `verify` 的 argv 接到 `python3.11 -B isolation_examples.py verify`，使它运行完整原回归和联合核账，任何失败都以非零状态退出。将 `demo` 的 argv 接到 `python3.11 -B isolation_examples.py demo`，打印两种归责在关闭重开后读回的真实明细、拒绝与调用轨迹。交付时简述执行如何交接两份已裁定的数据，以及发布和读取如何保持它们属于同一固定运行。
