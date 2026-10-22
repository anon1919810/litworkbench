import { Modal } from './Modal';
import { useAppState } from '../state/AppState';

/** 如实说明本里程碑做了什么、没做什么。不展示演示数据，也不暗示尚未实现的 AI 能力。 */
export function AboutDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { workspace } = useAppState();
  return (
    <Modal
      open={open}
      title="本版本的范围与限制"
      note="个人图书馆 · 本地文献工作台（第一里程碑）"
      onClose={onClose}
      footer={
        <button type="button" className="btn" onClick={onClose}>
          关闭
        </button>
      }
    >
      <section style={{ marginBottom: 18 }}>
        <h3 className="set-h">已经接通本地服务</h3>
        <ul className="plan-list" style={{ marginTop: 8 }}>
          <li>
            批量导入 PDF / DOCX
            <span className="plan-sub">文件只上传到本机 127.0.0.1 的服务，不发送到任何第三方。</span>
          </li>
          <li>
            后台逐页文字识别与全文索引
            <span className="plan-sub">进度来自服务端持久化任务状态；关闭浏览器不中止本机任务。</span>
          </li>
          <li>
            全库检索（精确短语 / 全部包含 / 任一关键词）
            <span className="plan-sub">只做字面匹配，不把语义相近当作原文命中。</span>
          </li>
          <li>
            原页核对（原件 / 识别文字 / 对照）与人工校订
            <span className="plan-sub">文件页序与印刷页码分开记录；印刷页码需人工确认。</span>
          </li>
          <li>
            保存摘录（精确原文切片 + 来源定位 + 分类与备注）
            <span className="plan-sub">偏移取自你在阅读区实际选中的那一次出现。</span>
          </li>
        </ul>
      </section>
      <section style={{ marginBottom: 18 }}>
        <h3 className="set-h">本里程碑不做</h3>
        <ul className="plan-list" style={{ marginTop: 8 }}>
          <li>
            别名扩展与语义检索
            <span className="plan-sub">界面不放置虚假扩展按钮；只有字面命中才算命中。</span>
          </li>
          <li>
            AI 研究项目与模型配置
            <span className="plan-sub">本页不接收密钥，也不发起任何模型调用。</span>
          </li>
          <li>
            摘录集整理、批量分类与报告导出
            <span className="plan-sub">第二里程碑内容；摘录本身现在就能保存与核验。</span>
          </li>
          <li>
            OCR 框级高亮
            <span className="plan-sub">只有本地服务返回真实行坐标时才绘制；没有坐标就不画。</span>
          </li>
        </ul>
      </section>
      <section>
        <h3 className="set-h">数据位置</h3>
        <dl className="kv-l" style={{ marginTop: 8 }}>
          <dt>工作区</dt>
          <dd>{workspace?.root || '—'}</dd>
          <dt>会话</dt>
          <dd>随页面加载建立的 HttpOnly 会话 cookie；写请求令牌只保存在内存中。</dd>
          <dt>网络</dt>
          <dd>仅访问本机同源接口；不加载外部字体、脚本或统计服务。</dd>
        </dl>
      </section>
    </Modal>
  );
}