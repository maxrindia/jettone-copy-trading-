"use client";

import { SessionProvider } from "next-auth/react";
import { ReactNode } from "react";
import { SidebarVisibilityProvider } from "@/components/layout/SidebarVisibility";

export default function Providers({ children }: { children: ReactNode }) {
  return (
    <SessionProvider>
      <SidebarVisibilityProvider>{children}</SidebarVisibilityProvider>
    </SessionProvider>
  );
}
