# Codex SuperAgent

[English](README.md) · [安装](#开始使用) · [架构说明](docs/architecture.md) · [运行时指南](skills/codex-superagent/references/runtime.zh-CN.md) · [MIT 许可](LICENSE)

Codex SuperAgent 将项目级任务拆分、完整任务负责制、持久交接和本机看板组织成可复用的 Codex skill。**Router／协调主对话对“整个项目怎样拆分、哪些工作适合并行”负责，Worker 对分配后的完整技术任务负责。**

## 与 DeepSeek 原始 SuperAgent 的区别

本项目参考 DeepSeek 的公开研究原型。下面的比较以固定版本的默认角色提示和执行模型为依据。

| 维度 | DeepSeek 公开原型 | 本项目的 Codex 工作方式 |
| --- | --- | --- |
| 工作入口 | GitHub、飞书等来源的请求，以及已启用规则产生的候选，形成授权工单 | 用户给出项目目标或剩余计划，协调主对话判断如何组织开发 |
| 主协调角色 | 主 Session 研判请求、确定工单归属并转达结果，默认提示将实现规划交给 Executor | Router 还负责项目级技术拆分，判断耦合、依赖、写入归属、并行可行性和交付边界 |
| 技术执行 | Executor 负责工单调查与实现，可使用内部子智能体 | Worker 负责完整任务的调查、实现方案、代码、接线、验证和返修，可在授权内内部委派 |
| 执行承载 | DeepSeek Harness、来源订阅和原型运行时 | Codex 可用的聊天／子智能体工具、技能规则及本地持久路由组件 |

DeepSeek 的默认分派提示将主 Session 的工作限定在请求研判与路由，将具体技术规划交给执行者。原型同时支持主动候选研判，包含授权、意图和范围判断。参考其[主角色提示](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/prompts/orchestrator.md)、[分派规则](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/prompts/dispatch.md)与[候选准入](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/prompts/admission.md)。

**两种工作负担会影响模型选择。** 边界已清楚的工单路由承担较少的项目技术规划。本项目的 Router 需要理解模块关系与共享状态，判断拆分后的协调成本，因此应具备胜任这些技术判断的推理能力。这个差异是依据职责作出的设计判断，模型选型和效率收益需要结合实际任务验证。

## 项目级拆分与技术实现规划

- **Router 决定任务怎样分。** 例如邮件窗口与背包能否独立开发，共用会话状态由谁统一修改，前后依赖怎样排序，手机验收由谁合批。
- **Worker 决定一项任务怎样实现。** 例如邮件窗口需要修改哪些代码、如何组织状态和接口、怎样处理错误，以及用什么检查证明完成。

Router 根据真实需求、代码入口和已有证据作决定。事实不足时，可以让 Worker 做一次有明确范围的调查，再据此决定拆分。任务内部的普通实现决定和技术验收由其 Owner 收口。

## 怎样避免拆分反而拖慢开发

| 已核实的条件 | 分工方式 |
| --- | --- |
| 多项修改依赖同一段可变状态、共享接口或连续保存流程 | 由一个 Owner 完整处理，或先完成共享接口再推进后续工作 |
| 界面可独立实现，输入和可写范围清楚，最终共用一部手机 | 独立开发各自交付，兼容的手机用例由现场负责人合批验收 |
| 对调用关系或实际缺口缺少证据 | 先做有界调查，Router 据事实确认任务边界 |
| 工作包已交付，整体验收仍有依赖 | 保留未验范围、责任者与解除事件，按原完成条件收口 |

并发数量跟随真正就绪的独立任务。用现有记录观察有效交付、资源等待、交接和返修成本，再调整分工。相关输入和条件未变的已通过结果可以复用。

## 职责与工作流

~~~mermaid
flowchart LR
    U[用户目标与原计划] --> R[Router 核事实、拆任务与协调依赖]
    R --> A[Worker A 完整任务]
    R --> B[Worker B 完整任务]
    E[已绑定任务的后续事件] --> A
    A --> D[证据、交付与剩余依赖]
    B --> D
    D --> R
    R --> U
~~~

Router 保留原始要求和整体计划，承担项目级拆分、任务归属及跨任务决策。Worker 负责任务内的方案、实施、共享接线、受影响验证、交付和返修。一个文件同一时刻只有一个技术写者，共享设备、账号和存档沿用项目互斥与保存入口。

等待、暂停、审批或返修中的未完成任务仍计入项目 Worker 上限。新事实改变任务边界时由 Router 调整分工，已有任务的普通跟进直接回到 Owner。用户明确选择单对话开发时，按该项目当前约定执行。

## 开始使用

向 Codex 的技能安装器发送以下地址。

~~~text
使用 $skill-installer 安装：
https://github.com/lishuoipad/codex-superagent/tree/main/skills/codex-superagent
~~~

给出具体项目和范围，从已有计划与证据开始。

~~~text
使用 $codex-superagent 审核这个项目剩余的开发计划。
由协调主对话负责项目级拆分和并行可行性判断。
先核共享状态、写入范围、依赖和独立交付条件，
复用当前已授权的参与者、测试入口和有效证据。
保留既有功能范围、验收标准和暂停要求。
~~~

新建独立对话需要明确请求，多对话通信需要覆盖参与者和用途的授权。首次建队的数量应结合就绪任务与真实宿主容量确定，原有团队按当前合同交接。安装位置以[Codex 技能文档](https://learn.chatgpt.com/docs/build-skills)及客户端为准，手工安装时复制完整的 `skills/codex-superagent` 目录。

## 已有能力与宿主接入

| 能力 | 实际承载 |
| --- | --- |
| 项目级拆分与耦合判断 | 技能指导协调主对话依据事实作技术判断 |
| 绑定、容量、暂停和事件去重 | 本地 SQLite 运行时及离线测试 |
| 原生聊天消息投递 | 适配器记录意图、结果与消费，授权调用方执行实际聊天工具 |
| 原计划接续 | 只读选择器检查现有计划、依赖和证据，Router 判断候选任务的边界与派发 |
| 标题与存储收尾 | 原交接中的标题读回，以及按项目要求记录的产物处置 |
| 本机看板 | 展示已落盘状态、公开进展及计划关联 |

路由状态检查和任务拆分质量分别验证。原生接续需要宿主提供可用接口并完成真实投递核验，跨工具调用分别留证。可选 RPC 路径使用原宿主已验证的控制接口。完整接入、暂停、未知发送及回访停止规则见[运行时指南](skills/codex-superagent/references/runtime.zh-CN.md)。

## 离线体验与验证

Python 3.10 及以上，路由示例和测试使用标准库。

~~~bash
git clone https://github.com/lishuoipad/codex-superagent.git
cd codex-superagent
python -B examples/offline_demo.py
python -B -m unittest discover -s tests -v
~~~

示例使用虚构身份和临时状态，开发开关保持暂停。离线测试覆盖运行时行为，拆分是否合适及长期开发效率依据真实任务中的交付、等待和返修记录核验。

## 仓库与来源

- [可安装技能](skills/codex-superagent/SKILL.md)及按需阅读的工作流、运行时和证据指南。
- [可选 Windows 看板](tools/development-status/README.md)及离线路由示例、测试。
- [架构说明](docs/architecture.md)、[来源记录](NOTICE)和 [MIT 许可](LICENSE)。

实现承接 [Agent Relay](https://github.com/lishuoipad/agent-relay) 的工程积累，设计参考 DeepSeek 的[领域模型](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/001-domain.md)与[执行模型](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/003-execution.md)。本项目由社区独立维护。贡献请提供可复现的协调问题、必要证据和受影响检查，公开示例使用虚构身份。
