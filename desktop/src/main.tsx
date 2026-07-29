import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "@sun-typeface/suit/fonts/variable/woff2/SUIT-Variable.css";
import "@fontsource-variable/jetbrains-mono";
import "./styles.css";
import "./styles-product-shell.css";
import "./styles-compact-secondary.css";
import "./styles-compact-analysis-labeling.css";
import "./styles-visual-polish.css";

const root = document.getElementById("root");

if (!root) {
  throw new Error("TrafficVisson renderer root is missing.");
}

createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
