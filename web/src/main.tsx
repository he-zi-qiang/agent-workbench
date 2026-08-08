import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import { Providers } from "./app/Providers";
import "./styles/tokens.css";
import "./styles/app.css";
import "./styles/minimal-theme.css";

const root = document.getElementById("root");
if (root === null) throw new Error("missing #root");

createRoot(root).render(
  <StrictMode>
    <Providers>
      <App />
    </Providers>
  </StrictMode>,
);
