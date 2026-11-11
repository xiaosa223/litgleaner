// 页面里任何一处出错都不该让整个界面变成空白：显示可读的提示和恢复办法。
import { Component, type ErrorInfo, type ReactNode } from "react";

export class ErrorBoundary extends Component<{ children: ReactNode }, { message: string | null }> {
  state = { message: null as string | null };

  static getDerivedStateFromError(error: unknown): { message: string } {
    return { message: error instanceof Error ? error.message : String(error) };
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    // 浏览器控制台是这个本机应用唯一能留下错误现场的地方
    console.error("界面出错", error, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.message === null) return this.props.children;
    return (
      <div className="empty" role="alert" style={{ paddingTop: "20vh" }}>
        <h2>这个页面出错了</h2>
        <p>你的笔记和阅读进度都已经保存，不受影响。刷新后可以继续。</p>
        <p className="tag">{this.state.message}</p>
        <button className="primary" onClick={() => window.location.reload()}>刷新页面</button>{" "}
        <button className="ghost" onClick={() => { window.location.href = "/"; }}>回到首页</button>
      </div>
    );
  }
}
