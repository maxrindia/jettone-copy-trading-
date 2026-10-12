import Sidebar from "@/components/layout/Sidebar";
import Header from "@/components/layout/Header";
import AppMain from "@/components/layout/AppMain";
import Providers from "@/components/providers/Providers";

export default function AppLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <Providers>
      <div className="flex h-[100dvh] overflow-hidden">
        <Sidebar />
        <div className="flex-1 flex flex-col min-w-0 h-full">
          <Header />
          <AppMain>{children}</AppMain>
        </div>
      </div>
    </Providers>
  );
}
