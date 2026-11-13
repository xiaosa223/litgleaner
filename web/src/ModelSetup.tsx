// 模型配置表单：设置页和“第一次用到模型时”的原地弹窗共用（第 15.5 节）。
// 先实际测试一次连接再保存；key 只提交给本机服务存进系统钥匙串，界面不回显。
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "./api";
import {
  applyPreset, formFromSettings, preset, PRESETS, probeCeiling, probeText, taskLabel, TASKS, toBody,
  type ModelForm, type ModelSettingsView, type ModelTask, type PresetId,
} from "./modelForm";
import { Modal } from "./ui";

const message = (error: unknown, fallback: string) => (error instanceof Error ? error.message : fallback);

function Field({ id, label, hint, children }: { id: string; label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="form-row">
      <label htmlFor={id}>{label}</label>
      {children}
      {hint && <div className="tag" id={`${id}-hint`}>{hint}</div>}
    </div>
  );
}

const Link = ({ href, children }: { href: string; children: ReactNode }) =>
  <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>;

export function ModelSetupForm({ focusTask, onSaved, onCancel }: {
  focusTask?: string; onSaved: (view: ModelSettingsView) => void; onCancel: () => void;
}) {
  const [form, setForm] = useState<ModelForm | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"test" | "save" | null>(null);
  const [status, setStatus] = useState<{ text: string; bad: boolean } | null>(null);
  const [failedProbe, setFailedProbe] = useState(false);
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);

  useEffect(() => {
    let alive = true;
    api.models().then(
      (view) => alive && setForm(formFromSettings(view)),
      (error: unknown) => alive && setLoadError(message(error, "读取模型设置失败")),
    );
    return () => { alive = false; };
  }, []);

  if (loadError) return <><p className="hint-line bad" role="alert">{loadError}</p><div className="dialog-actions"><button className="ghost" onClick={onCancel}>关闭</button></div></>;
  if (!form) return <p role="status" className="tag">正在读取当前设置…</p>;

  const item = preset(form.preset);
  const update = (patch: Partial<ModelForm>) => {
    setForm({ ...form, ...patch });
    setStatus(null);
    setFailedProbe(false);
  };
  const toggle = (task: ModelTask) =>
    update({ tasks: form.tasks.includes(task) ? form.tasks.filter((t) => t !== task) : [...form.tasks, task] });

  const save = async (skipTest: boolean) => {
    const result = toBody(form);
    if (!result.ok) { setStatus({ text: result.error, bad: true }); return; }
    try {
      if (!skipTest) {
        setBusy("test");
        setStatus({ text: "正在测试连接，通常几秒到半分钟…", bad: false });
        const probe = await api.testModel(result.body);
        if (!probe.ok) {
          setStatus({ text: probeText(probe), bad: true });
          setFailedProbe(true);
          return;
        }
        setStatus({ text: probeText(probe), bad: false });
      }
      setBusy("save");
      const saved = await api.saveModel(result.body);
      // 用户在测试或保存途中关掉了窗口：设置已保存，但不再继续刚才的操作
      if (mounted.current) onSaved(saved);
    } catch (error) {
      if (mounted.current) setStatus({ text: message(error, "保存失败"), bad: true });
    } finally {
      if (mounted.current) setBusy(null);
    }
  };

  return (
    <div className="model-setup">
      <div role="radiogroup" aria-label="模型提供方" className="preset-grid">
        {PRESETS.map((p) => (
          <button key={p.id} role="radio" aria-checked={form.preset === p.id} className="preset"
            onClick={() => form.preset !== p.id && update(applyPreset(form, p.id as PresetId))}>
            <b>{p.name}</b><span>{p.note}</span>
          </button>
        ))}
      </div>

      {item.id === "custom" && (
        <Field id="ms-url" label="接口地址" hint="写到 /v1 为止，例如 http://127.0.0.1:8000/v1">
          <input id="ms-url" aria-describedby="ms-url-hint" className="field" value={form.baseUrl} onChange={(e) => update({ baseUrl: e.target.value })} />
        </Field>
      )}
      <Field id="ms-key" label={item.needsKey ? "API key" : "API key（没有可以不填）"}
        hint={<>{form.keySaved ? `已保存，末四位 ${form.keyHint ?? "****"}；留空表示沿用。` : "只保存在这台电脑的系统钥匙串里。"}
          {item.keyUrl && <> 还没有 key？到 <Link href={item.keyUrl}>{item.name} 官网</Link> 注册后创建。</>}</>}>
        <input id="ms-key" aria-describedby="ms-key-hint" className="field" type="password" autoComplete="off" value={form.apiKey}
          placeholder={form.keySaved ? "••••（已保存）" : ""} onChange={(e) => update({ apiKey: e.target.value })} />
      </Field>
      <Field id="ms-model" label="模型名" hint={item.modelHint}>
        <input id="ms-model" aria-describedby="ms-model-hint" className="field" value={form.model} onChange={(e) => update({ model: e.target.value })} />
      </Field>
      <div className="form-pair">
        <Field id="ms-in" label="输入价格（美元/百万 token）">
          <input id="ms-in" className="field" inputMode="decimal" value={form.inputPrice} onChange={(e) => update({ inputPrice: e.target.value })} />
        </Field>
        <Field id="ms-out" label="输出价格（美元/百万 token）">
          <input id="ms-out" className="field" inputMode="decimal" value={form.outputPrice} onChange={(e) => update({ outputPrice: e.target.value })} />
        </Field>
      </div>
      <div className="tag" style={{ margin: "-6px 0 12px" }}>用来估算和限制花费。{item.priceUrl ? <>按 <Link href={item.priceUrl}>官网价格页</Link> 填写；</> : null}免费模型填 0；官网按人民币标价时除以汇率换成美元。</div>
      <Field id="ms-budget" label="每天最多花（美元）" hint="当天花到这个数后，用到模型的操作暂停到第二天（按 UTC 日界），不会多扣钱。">
        <input id="ms-budget" aria-describedby="ms-budget-hint" className="field" inputMode="decimal" value={form.dailyBudget} onChange={(e) => update({ dailyBudget: e.target.value })} />
      </Field>

      <details className="advanced">
        <summary>高级：哪些功能用这个模型、接口细节</summary>
        <p className="tag">默认所有功能都用这个模型。只想让部分功能用它时取消勾选其余项。</p>
        <div className="task-checks">
          {TASKS.map((t) => (
            <label key={t.task} title={t.hint}>
              <input type="checkbox" checked={form.tasks.includes(t.task)} onChange={() => toggle(t.task)} /> {t.label}
            </label>
          ))}
        </div>
        {item.id !== "custom" && (
          <Field id="ms-url2" label="接口地址（用中转站时改这里）">
            <input id="ms-url2" className="field" value={form.baseUrl} placeholder="官方默认地址" onChange={(e) => update({ baseUrl: e.target.value })} />
          </Field>
        )}
        <Field id="ms-structured" label="结构化输出" hint="接口不支持 JSON Schema 或测试失败时选“写进提示词”">
          <select id="ms-structured" aria-describedby="ms-structured-hint" className="pick" value={form.structured} onChange={(e) => update({ structured: e.target.value as ModelForm["structured"] })}>
            <option value="native">接口自带</option>
            <option value="prompt">写进提示词</option>
          </select>
        </Field>
        {form.protocol === "openai_chat" && (
          <Field id="ms-field" label="输出上限的字段名" hint="兼容接口大多认 max_tokens；测试失败且提示参数错误时换另一个">
            <select id="ms-field" aria-describedby="ms-field-hint" className="pick" value={form.maxTokensField} onChange={(e) => update({ maxTokensField: e.target.value as ModelForm["maxTokensField"] })}>
              <option value="">沿用当前设置</option>
              <option value="max_tokens">max_tokens</option>
              <option value="max_completion_tokens">max_completion_tokens（OpenAI 官方）</option>
            </select>
          </Field>
        )}
        <Field id="ms-tokens" label="单次输出上限（token）" hint="推理模型思考很长时调大，例如 16000">
          <input id="ms-tokens" aria-describedby="ms-tokens-hint" className="field" inputMode="numeric" value={form.maxOutputTokens} onChange={(e) => update({ maxOutputTokens: e.target.value })} />
        </Field>
      </details>

      {focusTask && !form.tasks.includes(focusTask as ModelTask) && (
        <p className="hint-line bad">没有勾选“{taskLabel(focusTask)}”，保存后刚才的操作仍然不能用。</p>
      )}
      <div className={`hint-line ${status?.bad ? "bad" : status ? "ok" : ""}`} role={status?.bad ? "alert" : "status"}>{status?.text}</div>
      <div className="dialog-actions">
        <button className="ghost" onClick={onCancel} disabled={busy !== null}>取消</button>
        {failedProbe && <button className="ghost" disabled={busy !== null} onClick={() => void save(true)}>仍然保存</button>}
        <button className="primary" disabled={busy !== null} onClick={() => void save(false)}>
          {busy === "test" ? "正在测试…" : busy === "save" ? "正在保存…" : "测试并保存"}
        </button>
      </div>
      <div className="tag">
        测试会真实调用模型一到两次，每次输出不超过 1024 token
        {probeCeiling(form) !== null && `，按填写的价格最多约 $${probeCeiling(form)!.toFixed(4)}`}。
      </div>
    </div>
  );
}

type Require = (task: string, resume?: () => void) => void;
const ModelSetupContext = createContext<Require>(() => undefined);
/** 遇到“还没有配置模型”时调用：弹出配置，保存后执行 resume 继续刚才的操作。 */
export const useModelSetup = () => useContext(ModelSetupContext);

export function ModelSetupProvider({ children }: { children: ReactNode }) {
  // 配置前可能有几处操作都被拒（例如连点了几段翻译）：保存后全部继续
  const [request, setRequest] = useState<{ task: string; resumes: (() => void)[] } | null>(null);
  const require = useCallback<Require>((task, resume) => setRequest((current) => ({
    task: current?.task ?? task,
    resumes: [...(current?.resumes ?? []), ...(resume ? [resume] : [])],
  })), []);
  const close = useCallback(() => setRequest(null), []);
  return (
    <ModelSetupContext.Provider value={require}>
      {children}
      {request && (
        <Modal title="先配置一个模型" onClose={close} width={560}>
          <p className="d">
            {request.task ? `“${taskLabel(request.task)}”` : "这项操作"}需要调用大模型。配置一次，以后用到模型的功能都会用它。
            打开 PDF、高亮、笔记、检索不需要模型，不受影响。
          </p>
          <ModelSetupForm focusTask={request.task} onCancel={close}
            onSaved={() => { const resumes = request.resumes; close(); resumes.forEach((resume) => resume()); }} />
        </Modal>
      )}
    </ModelSetupContext.Provider>
  );
}
