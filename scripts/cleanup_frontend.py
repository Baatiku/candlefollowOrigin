"""Strip dead legacy UI code from frontend/src/App.jsx."""
from pathlib import Path


def drop_between(text: str, start: str, end: str) -> str:
    i = text.find(start)
    if i == -1:
        return text
    j = text.find(end, i)
    if j == -1:
        return text
    return text[:i] + text[j:]


def main() -> None:
    p = Path(__file__).resolve().parents[1] / "frontend" / "src" / "App.jsx"
    text = p.read_text(encoding="utf-8")
    text = text.replace(", Brain", "")

    dead_states = [
        "  const [aiKeyInput, setAiKeyInput] = useState('');\n",
        "  const [aiKeyVisible, setAiKeyVisible] = useState(false);\n",
        "  const [aiSaveMsg, setAiSaveMsg] = useState('');\n",
        "  const [backtest, setBacktest] = useState(null);\n",
        "  const [backtestLoading, setBacktestLoading] = useState(false);\n",
        "  const [learnLoading, setLearnLoading] = useState(false);\n",
        "  const [aiComparison, setAiComparison] = useState(null);\n",
        "  const [aiLoading, setAiLoading] = useState(false);\n",
        "  const [optLogs, setOptLogs] = useState(null);\n",
        "  const [optLoading, setOptLoading] = useState(false);\n",
        "  const [evalLogs, setEvalLogs] = useState(null);\n",
        "  const [heatmap, setHeatmap] = useState(null);\n",
        "  const [gateLog, setGateLog] = useState([]);\n",
        "  const [assetBreakdown, setAssetBreakdown] = useState(null);\n",
        "  const [sequentialAmountsInput, setSequentialAmountsInput] = useState('');\n",
        "  const [dailyPnl, setDailyPnl] = useState(null);\n",
        "  const [newTokenDays, setNewTokenDays] = useState(30);\n",
        "  const [newTokenKey, setNewTokenKey] = useState('');\n",
        "  const [copiedToken, setCopiedToken] = useState('');\n",
        "  const [saveMessage, setSaveMessage] = useState('');\n",
    ]
    for s in dead_states:
        text = text.replace(s, "")

    text = drop_between(text, "  const createToken = async () => {", "  const fetchTodAnalytics = useCallback(async () => {")
    text = drop_between(text, "  const loadOptLogs = useCallback(async () => {", "  const handleAccountChange = async (e) => {")
    text = drop_between(text, "  const saveAiSettings = async ({ keys, enabled, shadowMode } = {}) => {", "  const initTierEditor = () => {")
    text = drop_between(text, "  const loadAiComparison = async () => {", "  const exportTradeHistory = async (format = 'json') => {")
    text = drop_between(text, "  const exportAiComparison = () => {", "  const handleWizardSubmit = async (e) => {")

    text = text.replace("Pair not suitable for straddle", "Pair quality check failed")
    text = text.replace("Straddle OK · ER", "Pair OK · ER")
    text = text.replace("AI rules active", "Learned gates (display only)")
    text = text.replace(
        "Each trade logs bot confidence, ER, slope, straddle score, alignment, and entry snapshot metrics for later analysis.",
        "Each trade logs candle-follow direction, confidence, and entry snapshot metrics for later analysis.",
    )
    text = text.replace(
        "<th style={{ padding: '0.3rem 0.4rem' }}>Straddle</th>",
        "<th style={{ padding: '0.3rem 0.4rem' }}>Score</th>",
    )
    text = text.replace(
        "entry_straddle_score ?? snap.straddle_score",
        "entry_straddle_score ?? snap.movement_score ?? snap.straddle_score",
    )
    text = text.replace("Straddle: ", "Score: ")
    text = text.replace(
        "Click any trade row to expand its full reasoning — slope, ER, guards, direction logic, and AI gate decision.",
        "Click any trade row to expand entry metrics and outcome details.",
    )

    p.write_text(text, encoding="utf-8")
    print(f"Cleaned {p}")


if __name__ == "__main__":
    main()
