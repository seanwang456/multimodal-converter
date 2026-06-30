import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "多模态文件转换",
  description: "支持文档、图片、音频、视频的格式转换与智能内容提取",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen bg-slate-50 text-slate-900">{children}</body>
    </html>
  );
}
