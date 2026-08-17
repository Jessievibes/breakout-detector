import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Breakout Detector',
  description: 'Newly released apps ranked by growth velocity',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
