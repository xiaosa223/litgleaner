// 界面预览：尚未接线的页面（课题调研、知识库、今日复习）用示例数据展示界面。
// 发布前把默认值改为 false，并随各接口接上逐个删除示例数据（见 ROADMAP 的已知差异）。
import { usePref } from "../prefs";

export const PREVIEW_DEFAULT = true;

export function usePreview(): [boolean, (on: boolean) => void] {
  return usePref<boolean>("preview", PREVIEW_DEFAULT);
}
