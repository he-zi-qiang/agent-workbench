import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type PropsWithChildren, useState } from "react";
import { IdentityProvider } from "./IdentityContext";

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
      <IdentityProvider>{children}</IdentityProvider>
    </QueryClientProvider>
  );
}
