import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import App from "./App"
import Phone from "./phone"
import "./index.css"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {location.pathname.startsWith("/app") ? <Phone /> : <App />}
  </StrictMode>,
)
