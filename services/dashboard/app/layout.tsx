import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "AI SRE Analyst — Mission Control",
  description: "GenAI-powered incident analysis for LLM serving infrastructure on Kubernetes",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600&family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500;700&display=swap"
          rel="stylesheet"
        />
      </head>
      <body className="bg-ink-950 text-zinc-200 font-sans antialiased min-h-screen">
        {children}
      </body>
    </html>
  );
}
