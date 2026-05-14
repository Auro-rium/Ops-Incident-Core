import type { ReactNode } from "react";

export const metadata = {
  title: "IncidentOps Agent",
  description: "Incident investigation copilot demo",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body style={{ margin: 0, fontFamily: "Arial, sans-serif", background: "#f6f7fb", color: "#111827" }}>
        {children}
      </body>
    </html>
  );
}
