import "../styles.css";
import { loadPresentationConfig } from "./config.js";

await loadPresentationConfig();
await import("./family-archive-app.js");
