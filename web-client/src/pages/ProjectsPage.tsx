import { IconProjects } from '../components/Icons';
import { PageHeader } from '../components/PageHeader';
import { Tag } from '../components/Tag';
import { useAppState } from '../state/AppState';

/**
 * 研究项目属于后续里程碑。这里只说明将来会做什么，
 * 不展示任何演示项目、假进度或模型输出。
 */
export function ProjectsPage() {
  const { navigate } = useAppState();

  return (
    <div className="page">
      <PageHeader
        icon={<IconProjects size={30} />}
        title="研究项目"
        description="围绕一个专题组织文献、摘录与报告。当前版本尚未实现，本页只说明范围，不生成任何内容。"
      />
      <div className="page-body">
        <div className="set-block">
          <h2 className="set-h">计划中的能力</h2>
          <p className="set-sub">以下都是尚未实现的计划，本版本没有对应的后端接口，界面上也不会放置假的运行按钮。</p>
          <ul className="plan-list">
            <li>
              项目记录：问题、选用文献、方案与报告
              <span className="plan-sub">启动研究时把范围、分类、别名与提示词快照保存，便于复现。</span>
            </li>
            <li>
              受范围约束的检索与阅读
              <span className="plan-sub">search / read / excerpt / finish 全过程都只使用项目选定的文献。</span>
            </li>
            <li>
              进度显示真实动作
              <span className="plan-sub">只显示“检索、阅读、保存证据、形成报告”等可观测步骤，不伪造模型思考日志。</span>
            </li>
            <li>
              导出报告
              <span className="plan-sub">Markdown / JSON / DOCX 报告在第二里程碑，与摘录导出一起做。</span>
            </li>
          </ul>
        </div>

        <div className="set-block">
          <h2 className="set-h">现在可以做的事</h2>
          <p className="set-sub">这些都已经在本版本里可用，并且都是真实数据：</p>
          <ul className="plan-list">
            <li>
              全库检索 + 原页核对 + 保存摘录
              <span className="plan-sub">检索页右侧的阅读区可以对任意一页保存精确引文。</span>
            </li>
            <li>
              摘录的分类、备注与核验
              <span className="plan-sub">摘录集里可以按状态与来源筛选、标记已接受或已拒绝。</span>
            </li>
          </ul>
          <div className="btn-row" style={{ marginTop: 12 }}>
            <button type="button" className="btn btn-sm btn-primary" onClick={() => navigate('search')}>
              回到全库检索
            </button>
            <button type="button" className="btn btn-sm" onClick={() => navigate('excerpts')}>
              打开摘录集
            </button>
            <Tag tone="neutral">未实现</Tag>
          </div>
        </div>
      </div>
    </div>
  );
}