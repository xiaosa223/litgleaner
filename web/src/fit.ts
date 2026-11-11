// 全文覆盖：译文要放进原文段落的方框里。中文通常比英文短，但术语多时会更长，所以按方框高度缩小字号。

const STEP = 0.25;
const BASE_RATIO = 10.5 / 612; // US Letter 宽 612pt，正文约 10pt，译文略大一点便于阅读

/** 页面宽度（像素）对应的译文基准字号。 */
export function overlayBaseSize(pageWidth: number): number {
  return pageWidth * BASE_RATIO;
}

/** 在 [min, max] 中找放得下的最大字号（精度 0.25 像素）；最小字号也放不下时返回最小字号，由调用方允许溢出滚动。 */
export function fitFontSize(fits: (size: number) => boolean, max: number, min: number): number {
  if (max <= min) return min;
  if (fits(max)) return max;
  let lo = min, hi = max; // 不变式：hi 放不下
  while (hi - lo > STEP) {
    const mid = (lo + hi) / 2;
    if (fits(mid)) lo = mid;
    else hi = mid;
  }
  return lo;
}
