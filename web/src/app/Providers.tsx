import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type PropsWithChildren, useState } from "react";
import { IdentityProvider } from "./IdentityContext";
import { ThemeProvider } from "./ThemeContext";

export function Providers({ children }: PropsWithChildren) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 5_000,
            retry: (failureCount, error) => {
              if (error instanceof Error && "status" in error) {
                const status = Number(error.status);
                if (status >= 400 && status < 500) return false;
              }
              return failureCount < 2;
            },
          },
          mutations: { retry: false },
        },
      }),
  );
  return (
    <QueryClientProvider client={queryClient}>
      {/* 主题在身份外面。身份变化会让 AppShell 整棵 Outlet 重挂载
          （见 AppShell 的 identityKey），而主题不该跟着那次重挂载走一遍
          "读 localStorage → 写 data-theme"——那正是切换身份时会闪一下的原因。 */}
      <ThemeProvider>
        <IdentityProvider>{children}</IdentityProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
