// 快捷键提示按操作系统显示修饰键（第 12.8 节）。
export function isMac(platform: string, userAgent: string): boolean {
  return /mac|iphone|ipad/i.test(platform) || /mac os x|macintosh/i.test(userAgent);
}

export const modKey = (mac: boolean): string => (mac ? "⌘" : "Ctrl");

export const MAC = typeof navigator !== "undefined" && isMac(navigator.platform ?? "", navigator.userAgent ?? "");
export const MOD = modKey(MAC);
