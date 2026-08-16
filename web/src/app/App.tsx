import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { HashRouter } from "react-router-dom";
import { LoadingLine } from "../components/ui";
import { AppShell } from "./AppShell";
import { WorkbenchLayout } from "./WorkbenchLayout";

const ChatPage = lazy(() =>
  import("../features/chat/ChatPage").then(({ ChatPage }) => ({ default: ChatPage })),
);
const WorkPage = lazy(() =>
  import("../features/work/WorkPage").then(({ WorkPage }) => ({ default: WorkPage })),
);
const CodePage = lazy(() =>
  import("../features/code/CodePage").then(({ CodePage }) => ({ default: CodePage })),
);
const KnowledgePage = lazy(() =>
  import("../features/knowledge/KnowledgePage").then(({ KnowledgePage }) => ({
    default: KnowledgePage,
  })),
);
const EvaluationPage = lazy(() =>
  import("../features/evaluation/EvaluationPage").then(({ EvaluationPage }) => ({
    default: EvaluationPage,
  })),
);
const SystemPage = lazy(() =>
  import("../features/system/SystemPage").then(({ SystemPage }) => ({
    default: SystemPage,
  })),
);

export function App() {
  return (
    <HashRouter>
      <Suspense
        fallback={
          <div className="aw-app-loading">
            <LoadingLine label="正在打开工作台" />
          </div>
        }
      >
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<Navigate replace to="/chat" />} />
            {/* Pathless: it adds a tab strip and no URL segment, so every
                path below is exactly what it was before. */}
            <Route element={<WorkbenchLayout />}>
              <Route path="chat" element={<ChatPage />} />
              <Route path="chat/:sessionId" element={<ChatPage />} />
              <Route path="work" element={<WorkPage />} />
              <Route path="work/:taskId" element={<WorkPage />} />
            </Route>
            <Route path="code" element={<CodePage />} />
            <Route path="code/:sessionId" element={<CodePage />} />
            <Route path="knowledge" element={<KnowledgePage />} />
            <Route path="evaluation" element={<EvaluationPage />} />
            <Route path="system" element={<SystemPage />} />
            <Route path="*" element={<Navigate replace to="/chat" />} />
          </Route>
        </Routes>
      </Suspense>
    </HashRouter>
  );
}
