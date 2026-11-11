// 课题调研的三块结果：调研进度与覆盖度、阅读路线、课题地图（第 7.5 节）。
// 后端还没有调研接口，现在显示的是示例数据，每块都带“示例数据”说明。
import { SAMPLE_MAP, SAMPLE_ROUTE, SAMPLE_SURVEY, SAMPLE_TOPIC } from "../preview/sample";
import { PreviewNote } from "../ui";

const COVERED_BELOW = 0.05; // 最近一轮新增比例低于它，就提示“已基本覆盖”
const pct = (value: number) => `${(value * 100).toFixed(1)}%`;

export function SurveyTab({ onExpand }: { onExpand: () => void }) {
  const survey = SAMPLE_SURVEY;
  const last = survey.rounds[survey.rounds.length - 1];
  const ratio = last.candidates > 0 ? last.passed / last.candidates : 0;
  const covered = ratio < COVERED_BELOW;
  const seen = survey.rounds.reduce((sum, r) => sum + r.fresh, 0);
  return (
    <>
      <PreviewNote>下面是“{SAMPLE_TOPIC}”的示例，不是这个课题的真实结果。</PreviewNote>
      <div className="stats">
        <div className="stat"><b>{seen}</b><span>找到的候选论文</span></div>
        <div className="stat"><b>{survey.passed}</b><span>通过初筛，与课题相关</span></div>
        <div className="stat"><b>{survey.read}</b><span>已读</span></div>
        <div className={`stat${covered ? " good" : ""}`}><b>{pct(ratio)}</b><span>最近一轮里新的相关论文</span></div>
      </div>
      <div className="pcard" style={{ marginTop: 12 }}>
        <div className="pcard-top">
          <div style={{ flex: 1 }}>
            <b>{covered ? "已基本覆盖" : "建议再扩展一轮"}</b>
            <div className="pcard-by">
              {covered
                ? `第 ${last.round} 轮的 ${last.candidates} 篇候选里，只有 ${last.passed} 篇是以前没见过的相关论文。再扩展一轮找不到多少新论文。`
                : `第 ${last.round} 轮还找到了 ${last.passed} 篇新的相关论文，顺着它们的引用可能还有。`}
            </div>
          </div>
          <button className="ghost" onClick={onExpand} title="执行前会先显示预计篇数和费用">再扩展一轮</button>
        </div>
      </div>

      <h2 className="block-title">每一轮</h2>
      <table className="plain">
        <thead><tr><th>轮次</th><th>范围</th><th className="num">候选</th><th className="num">以前没见过</th><th className="num">其中相关</th><th className="num">费用</th></tr></thead>
        <tbody>
          {survey.rounds.map((r) => (
            <tr key={r.round}>
              <td>第 {r.round} 轮</td><td>{r.scope}</td>
              <td className="num">{r.candidates}</td><td className="num">{r.fresh}</td><td className="num">{r.passed}</td>
              <td className="num">${r.cost_usd.toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="two" style={{ marginTop: 20 }}>
        <div className="panel-card">
          <h4>检索了哪些来源</h4>
          {survey.sources.map((s) => <div className="src-row" key={s.name}><span>{s.name}</span><span>{s.found} 篇</span></div>)}
        </div>
        <div className="panel-card">
          <h4>没有覆盖的范围</h4>
          <ul>{survey.gaps.map((gap) => <li key={gap}>{gap}</li>)}</ul>
          <div className="tag" style={{ marginTop: 6 }}>覆盖度只说明在上面这些来源里还能找到多少，不代表“全了”。</div>
        </div>
      </div>
    </>
  );
}

export function RouteTab() {
  return (
    <>
      <PreviewNote>下面是“{SAMPLE_TOPIC}”的示例，不是这个课题的真实结果。</PreviewNote>
      {SAMPLE_ROUTE.map((group) => (
        <section key={group.key}>
          <div className="route-head"><b>{group.label}</b><span>{group.note}</span></div>
          {group.papers.length === 0 && <div className="empty-note">这一组还没有论文。</div>}
          {group.papers.map((p, i) => (
            <div className="route-item" key={p.title}>
              <span className="no">{i + 1}</span>
              <div>
                <div className="t">{p.title}</div>
                <div className="pcard-by">{p.by} · {p.venue} · {p.year}</div>
                <div className="why"><span className="badge b-model">模型概括</span> {p.why}</div>
              </div>
              <span className="cited">课题内被引 {p.cited} 次</span>
            </div>
          ))}
        </section>
      ))}
    </>
  );
}

export function MapTab() {
  return (
    <>
      <PreviewNote>下面是“{SAMPLE_TOPIC}”的示例，不是这个课题的真实结果。</PreviewNote>
      <div className="map-grid">
        {SAMPLE_MAP.groups.map((group) => (
          <div className="panel-card map-card" key={group.name}>
            <h4>{group.name}<span>{group.count} 篇</span></h4>
            <p><span className="badge b-model">模型概括</span> {group.note}</p>
            <ul>{group.titles.map((title) => <li key={title}>{title}</li>)}</ul>
          </div>
        ))}
      </div>
      {SAMPLE_MAP.ungrouped > 0 && <p className="quiet">另有 {SAMPLE_MAP.ungrouped} 篇没有归入任何子方向。</p>}
    </>
  );
}
