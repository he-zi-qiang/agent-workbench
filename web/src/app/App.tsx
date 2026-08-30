import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { HashRouter } from "react-router-dom";
import { LoadingLine } from "../components/ui";
import { AppShell } from "./AppShell";

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
const ComputerPage = lazy(() =>
  import("../features/computer/ComputerPage").then(({ ComputerPage }) => ({
    default: ComputerPage,
  })),
);
const UsagePage = lazy(() =>
  import("../features/usage/UsagePage").then(({ UsagePage }) => ({
    default: UsagePage,
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
            <Route path="chat" element={<ChatPage />} />
            <Route path="chat/:sessionId" element={<ChatPage />} />
            <Route path="work" element={<WorkPage />} />
            <Route path="work/:taskId" element={<WorkPage />} />
            {/* One route with an optional param, not two sibling routes: the
                first send navigates /code → /code/:id mid-turn, and two Route
                entries remount the page across that boundary -- dropping the
                `running` flag exactly while the first turn runs, so the
                composer re-enabled against an open request. */}
            <Route path="code/:sessionId?" element={<CodePage />} />
            <Route path="knowledge" element={<KnowledgePage />} />
            <Route path="evaluation" element={<EvaluationPage />} />
            <Route path="computer" element={<ComputerPage />} />
            <Route path="usage" element={<UsagePage />} />
            <Route path="system" element={<SystemPage />} />
            <Route path="*" element={<Navigate replace to="/chat" />} />
          </Route>
        </Routes>
      </Suspense>
    </HashRouter>
  );
}
