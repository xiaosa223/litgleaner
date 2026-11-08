// shadcn 约定的类名合并：后写的 Tailwind 类覆盖先写的同类。
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
