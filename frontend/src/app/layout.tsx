import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title:       'PlanLift',
  description: 'AI-powered floor plan to 3D — powered by Mask R-CNN & CubiCasa',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="h-screen w-screen overflow-hidden">{children}</body>
    </html>
  );
}
