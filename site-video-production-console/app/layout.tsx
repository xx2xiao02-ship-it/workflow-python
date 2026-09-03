import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "视频制作控制台",
  description: "从文案创作到 Windows 剪映草稿的分层制作控制台。",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
