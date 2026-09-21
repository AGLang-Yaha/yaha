const { Plugin, Modal, Setting, Notice, PluginSettingTab } = require("obsidian");
const child_process = require("child_process");
const path = require("path");
const fs = require("fs");

const DEFAULT_SETTINGS = {
    venvPath: "",
    customYahaPath: "",
    deepseekApiKey: "",
    geminiApiKey: "",
    openaiApiKey: ""
};

class YahaAgentsPlugin extends Plugin {
    async onload() {
        console.log("Loading YAHA Agents plugin...");
        await this.loadSettings();

        // Register the right-click editor context menu item
        this.registerEvent(
            this.app.workspace.on("editor-menu", (menu, editor, view) => {
                menu.addItem((item) => {
                    item.setTitle("YAHA Agent")
                        .setIcon("lucide-bot")
                        .onClick(async () => {
                            const selection = editor.getSelection();
                            const file = view.file;
                            new YahaAgentModal(this.app, this, editor, selection, file).open();
                        });
                });
            })
        );

        // Register a command to invoke the agent as well (accessible via Cmd+P palette)
        this.addCommand({
            id: "run-yaha-agent",
            name: "Invoke YAHA Agent",
            editorCallback: (editor, view) => {
                const selection = editor.getSelection();
                const file = view.file;
                new YahaAgentModal(this.app, this, editor, selection, file).open();
            }
        });

        // Register the Settings Tab
        this.addSettingTab(new YahaAgentsSettingTab(this.app, this));
    }

    onunload() {
        console.log("Unloading YAHA Agents plugin...");
    }

    async loadSettings() {
        this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
    }

    async saveSettings() {
        await this.saveData(this.settings);
    }
}

class YahaAgentsSettingTab extends PluginSettingTab {
    constructor(app, plugin) {
        super(app, plugin);
        this.plugin = plugin;
    }

    display() {
        const { containerEl } = this;
        containerEl.empty();

        containerEl.createEl("h2", { text: "YAHA Agents Configuration" });

        containerEl.createEl("h3", { text: "Environment & Paths" });

        new Setting(containerEl)
            .setName("Virtual Environment Path")
            .setDesc("The absolute path to the Python Virtual Environment where YAHA is installed (e.g. /Users/kuntan/code/claud/yaha-lite/.venv). Prepend its binary directory to PATH for execution.")
            .addText(text => text
                .setPlaceholder("/path/to/.venv")
                .setValue(this.plugin.settings.venvPath)
                .onChange(async (value) => {
                    this.plugin.settings.venvPath = value.trim();
                    await this.plugin.saveSettings();
                }));

        new Setting(containerEl)
            .setName("Custom YAHA Binary Path")
            .setDesc("The absolute path to the 'yaha' binary directly, if you wish to override standard PATH resolution (optional).")
            .addText(text => text
                .setPlaceholder("/usr/local/bin/yaha")
                .setValue(this.plugin.settings.customYahaPath)
                .onChange(async (value) => {
                    this.plugin.settings.customYahaPath = value.trim();
                    await this.plugin.saveSettings();
                }));

        containerEl.createEl("h3", { text: "API Keys" });

        new Setting(containerEl)
            .setName("DeepSeek API Key")
            .setDesc("Enter your DeepSeek API Key (overrides local env key).")
            .addText(text => {
                text.inputEl.type = "password";
                text.setPlaceholder("sk-...")
                    .setValue(this.plugin.settings.deepseekApiKey)
                    .onChange(async (value) => {
                        this.plugin.settings.deepseekApiKey = value.trim();
                        await this.plugin.saveSettings();
                    });
            });

        new Setting(containerEl)
            .setName("Gemini API Key")
            .setDesc("Enter your Google Gemini API Key (overrides local env key).")
            .addText(text => {
                text.inputEl.type = "password";
                text.setPlaceholder("AIzaSy...")
                    .setValue(this.plugin.settings.geminiApiKey)
                    .onChange(async (value) => {
                        this.plugin.settings.geminiApiKey = value.trim();
                        await this.plugin.saveSettings();
                    });
            });

        new Setting(containerEl)
            .setName("OpenAI API Key")
            .setDesc("Enter your OpenAI API Key (overrides local env key).")
            .addText(text => {
                text.inputEl.type = "password";
                text.setPlaceholder("sk-proj-...")
                    .setValue(this.plugin.settings.openaiApiKey)
                    .onChange(async (value) => {
                        this.plugin.settings.openaiApiKey = value.trim();
                        await this.plugin.saveSettings();
                    });
            });
    }
}

class YahaAgentModal extends Modal {
    constructor(app, plugin, editor, selection, file) {
        super(app);
        this.plugin = plugin;
        this.editor = editor;
        this.selection = this.normalizeSelection(selection);
        this.file = file;
        this.vaultPath = this.app.vault.adapter.getBasePath();
        this.agents = ["BaseAgent", "ChatAgent", "NoAgent"]; // Default fallback list
        this.selectedAgent = "BaseAgent";
        this.showDiff = false;
        this.replaceText = false;
        this.processRef = null;
    }

    /**
     * Normalizes the editor selection to a plain string. Some Obsidian editor
     * implementations return a String object or a {text, ...} shaped object.
     */
    normalizeSelection(selection) {
        if (selection == null) return "";
        if (typeof selection === "string") return selection;
        if (typeof selection === "object") {
            if (typeof selection.text === "string") return selection.text;
            if (typeof selection.content === "string") return selection.content;
            if (typeof selection.value === "string") return selection.value;
            if (typeof selection.toString === "function") {
                const str = selection.toString();
                if (str && str !== "[object Object]") return str;
            }
        }
        console.warn("YAHA Agents: unexpected selection type, using empty selection", selection);
        return "";
    }

    /**
     * Builds the command used to spawn YAHA.
     * On macOS/Linux this runs inside a login shell so the user's shell profile
     * (API keys, conda, etc.) is inherited. On Windows it falls back to a direct spawn.
     */
    buildCommand(commandBin = "yaha") {
        const yahaDir = path.join(this.vaultPath, ".yaha");
        const promptFilePath = path.join(yahaDir, "coauthor_prompt.txt");
        const resultFilePath = path.join(yahaDir, "coauthor_result.txt");

        const primaryArgs = [
            "-w", this.vaultPath,
            "-a", this.selectedAgent,
            "-F", promptFilePath,
            "-o", resultFilePath,
            "-y", // Yes-for-All ACS confirmations to prevent stdin hang
            "-f"  // Unattended mode (with -y and -o) so no prompt is expected
        ];

        const shellQuote = (arg) => "'" + String(arg).replace(/'/g, "'\\''") + "'";
        const useLoginShell = (process.platform === "darwin" || process.platform === "linux");

        const result = {
            commandBin,
            primaryArgs,
            promptFilePath,
            resultFilePath,
            useLoginShell,
            shellCommand: null,
            shellBin: null,
            shellArgs: null
        };

        if (useLoginShell) {
            const defaultShell = process.platform === "darwin" ? "/bin/zsh" : "/bin/bash";
            const shellBin = process.env.SHELL || defaultShell;
            const shellName = path.basename(shellBin).toLowerCase();

            let sourceFiles = [];
            if (shellName.includes("zsh")) {
                sourceFiles = ["~/.zshenv", "~/.zprofile", "~/.zshrc", "~/.zlogin"];
            } else if (shellName.includes("bash")) {
                sourceFiles = ["~/.bashrc", "~/.bash_profile", "~/.bash_login", "~/.profile"];
            }

            const escapedArgs = primaryArgs.map(shellQuote).join(" ");
            const escapedCommandBin = shellQuote(commandBin);
            const sourceCmd = sourceFiles.length
                ? `for f in ${sourceFiles.join(" ")}; do [ -f "$f" ] && source "$f" 2>/dev/null; done; `
                : "";
            const exportCmd = "export DEEPSEEK_API_KEY GEMINI_API_KEY OPENAI_API_KEY; ";
            const cmdString = `${sourceCmd}${exportCmd}${escapedCommandBin} ${escapedArgs}`;

            result.shellBin = shellBin;
            result.shellArgs = ["-l", "-i", "-c", cmdString];
            result.shellCommand = `${shellBin} -l -i -c ${cmdString}`;
        } else {
            result.shellBin = null;
            result.shellArgs = primaryArgs;
            result.shellCommand = `${commandBin} ${primaryArgs.join(" ")}`;
        }

        return result;
    }

    /**
     * Builds the environment object that will be passed to the child process.
     */
    async prepareEnv() {
        const env = { ...process.env };
        env.PYTHONUNBUFFERED = "1";

        // Automatically detect and parse .env file in vault root
        const dotenvPath = path.join(this.vaultPath, ".env");
        if (fs.existsSync(dotenvPath)) {
            try {
                const dotenvContent = fs.readFileSync(dotenvPath, "utf-8");
                const dotenvLines = dotenvContent.split("\n");
                for (let rawLine of dotenvLines) {
                    // Strip inline comments and trim
                    let line = rawLine;
                    const commentIdx = line.indexOf("#");
                    if (commentIdx >= 0) {
                        line = line.substring(0, commentIdx);
                    }
                    line = line.trim();
                    if (!line || line.includes("=") === false) continue;

                    const eqIdx = line.indexOf("=");
                    let key = line.substring(0, eqIdx).trim();
                    let val = line.substring(eqIdx + 1).trim();

                    // Strip optional 'export ' / 'set -x ' prefixes
                    if (key.startsWith("export ")) {
                        key = key.substring(7).trim();
                    } else if (key.startsWith("set -x ")) {
                        key = key.substring(7).trim();
                    }

                    // Strip matching surrounding quotes
                    if ((val.startsWith('"') && val.endsWith('"')) ||
                        (val.startsWith("'") && val.endsWith("'"))) {
                        val = val.substring(1, val.length - 1);
                    }

                    if (key) {
                        env[key] = val;
                    }
                }
            } catch (e) {
                console.error("Could not parse .env file:", e);
            }
        }

        // Inject configured API keys from settings if set
        if (this.plugin.settings.deepseekApiKey) {
            env.DEEPSEEK_API_KEY = this.plugin.settings.deepseekApiKey;
        }
        if (this.plugin.settings.geminiApiKey) {
            env.GEMINI_API_KEY = this.plugin.settings.geminiApiKey;
        }
        if (this.plugin.settings.openaiApiKey) {
            env.OPENAI_API_KEY = this.plugin.settings.openaiApiKey;
        }

        // Reconstruct path separators
        const isWindows = process.platform === "win32";
        const pathSep = isWindows ? ";" : ":";
        const binFolderName = isWindows ? "Scripts" : "bin";

        let activeVenvBin = null;

        // 1. Prioritize user's configured Virtual Environment from settings
        if (this.plugin.settings.venvPath) {
            const configPath = path.join(this.plugin.settings.venvPath, binFolderName);
            if (fs.existsSync(configPath)) {
                activeVenvBin = configPath;
            }
        }

        // 2. Fallback to scanning local Python virtual environment in vault folder (e.g. .venv or venv)
        if (!activeVenvBin) {
            const venvDirs = [".venv", "venv", "env", ".env"];
            for (const dir of venvDirs) {
                const checkPath = path.join(this.vaultPath, dir, binFolderName);
                if (fs.existsSync(checkPath)) {
                    activeVenvBin = checkPath;
                    break;
                }
            }
        }

        if (process.platform === "darwin") {
            const extraPaths = ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"];
            const currentPath = env.PATH || "";
            env.PATH = extraPaths.concat(currentPath.split(":")).filter(Boolean).join(":");
        }

        if (activeVenvBin) {
            const currentPath = env.PATH || "";
            env.PATH = activeVenvBin + pathSep + currentPath;
        }

        return env;
    }

    async onOpen() {
        const { contentEl } = this;
        contentEl.empty();
        contentEl.addClass("yaha-modal");

        // Load agents from workspace config dynamically
        await this.loadAgentsFromWorkspace();

        const titleEl = contentEl.createEl("h2", { text: "YAHA Agent Interface", cls: "yaha-modal-title" });
        titleEl.title = "Drag here to move the modal";

        // Make the Obsidian modal draggable and resizable (must run after title exists)
        try {
            this.setupModalDragResize();
        } catch (err) {
            console.error("YAHA setupModalDragResize failed:", err);
        }

        // Prevent the modal from disappearing when clicking outside it
        try {
            this.preventBackdropClose();
        } catch (err) {
            console.error("YAHA preventBackdropClose failed:", err);
        }

        const formContainer = contentEl.createDiv({ cls: "yaha-form-container" });

        // Agent Dropdown Row
        const agentRow = formContainer.createDiv({ cls: "yaha-form-row" });
        agentRow.createEl("label", { text: "Select Workspace Agent", cls: "yaha-form-label" });
        this.agentSelect = agentRow.createEl("select", { cls: "yaha-select" });
        this.agents.forEach(agent => {
            const opt = this.agentSelect.createEl("option", { text: agent, value: agent });
            if (agent === this.selectedAgent) {
                opt.selected = true;
            }
        });
        this.agentSelect.addEventListener("change", (e) => {
            this.selectedAgent = e.target.value;
        });

        // Re-read the live editor selection when the modal opens. The value
        // captured in the constructor is sometimes an object/stringified object.
        const liveSelection = (this.editor && typeof this.editor.getSelection === "function")
            ? this.editor.getSelection()
            : this.selection;
        this.selection = this.normalizeSelection(liveSelection);

        // Task Instructions Row (pre-filled with the selected text)
        const instrRow = formContainer.createDiv({ cls: "yaha-form-row" });
        instrRow.createEl("label", { text: "Task Instructions", cls: "yaha-form-label" });
        this.instrText = instrRow.createEl("textarea", {
            cls: "yaha-textarea",
            placeholder: "e.g., Rewrite this paragraph to sound more professional, translate to French, or generate a summary..."
        });
        this.instrText.value = this.selection;
        this.instrText.focus();

        // Final Result Row (populated after execution)
        const resultRow = formContainer.createDiv({ cls: "yaha-form-row" });
        resultRow.createEl("label", { text: "Final Result", cls: "yaha-form-label" });
        this.resultArea = resultRow.createEl("textarea", {
            cls: "yaha-textarea",
            style: "min-height: 120px; font-family: var(--font-monospace), monospace; font-size: 0.9em;"
        });
        // Final result box is editable so the user can tweak before applying
        this.resultArea.readOnly = false;
        this.resultArea.addEventListener("input", () => {
            this.resultText = this.resultArea.value;
            this.updateDiff();
        });

        // Checkbox row: Replace text + View diff
        const checkContainer = formContainer.createDiv({ cls: "yaha-checkbox-container" });

        const replaceLabel = checkContainer.createEl("label", { cls: "yaha-checkbox-label" });
        this.replaceCheckbox = replaceLabel.createEl("input", {
            type: "checkbox",
            cls: "yaha-checkbox"
        });
        this.replaceCheckbox.checked = this.replaceText;
        replaceLabel.appendText("Replace text");
        this.replaceCheckbox.addEventListener("change", (e) => {
            this.replaceText = e.target.checked;
        });

        const diffLabel = checkContainer.createEl("label", { cls: "yaha-checkbox-label" });
        this.viewDiffCheckbox = diffLabel.createEl("input", {
            type: "checkbox",
            cls: "yaha-checkbox"
        });
        this.viewDiffCheckbox.checked = this.showDiff;
        diffLabel.appendText("View diff");
        this.viewDiffCheckbox.addEventListener("change", (e) => {
            this.showDiff = e.target.checked;
            this.updateDiff();
        });

        // Running Live Console Area (Initially Hidden)
        this.consoleContainer = contentEl.createDiv({ cls: "yaha-console-container" });
        this.consoleContainer.style.display = "none";
        this.consoleContainer.createEl("div", { text: "Streaming Thoughts & Executions:", cls: "yaha-form-label" });
        this.consoleArea = this.consoleContainer.createEl("pre", { cls: "yaha-console" });

        // Diff Visualizer Container (Initially Hidden)
        this.diffContainer = contentEl.createDiv({ cls: "yaha-diff-container" });
        this.diffContainer.style.display = "none";
        this.diffContainer.createEl("div", { text: "Diff Preview (Red = Removed, Green = Added)", cls: "yaha-diff-header" });
        this.diffBody = this.diffContainer.createDiv({ cls: "yaha-diff-body" });

        // Button Group
        this.buttonGroup = contentEl.createDiv({ cls: "yaha-button-group" });
        const cancelBtn = this.buttonGroup.createEl("button", { text: "Cancel" });
        this.executeBtn = this.buttonGroup.createEl("button", { text: "Execute", cls: "mod-cta" });
        this.applyBtn = this.buttonGroup.createEl("button", { text: "Apply Changes", cls: "mod-cta" });
        this.applyBtn.style.display = "none";

        cancelBtn.addEventListener("click", () => {
            this.cleanupProcess();
            this.close();
        });

        this.executeBtn.addEventListener("click", async () => {
            const instructions = this.instrText.value.trim();
            if (!instructions) {
                new Notice("Please enter task instructions.");
                return;
            }

            // Lock UI
            this.executeBtn.disabled = true;
            this.instrText.disabled = true;
            this.agentSelect.disabled = true;
            this.replaceCheckbox.disabled = true;
            this.viewDiffCheckbox.disabled = true;

            // Start Execution
            await this.executeYahaTask(instructions);
        });

        this.applyBtn.addEventListener("click", () => {
            try {
                if (!this.editor) {
                    throw new Error("Editor reference is no longer available.");
                }

                const resultToApply = this.resultArea ? this.resultArea.value : this.resultText;
                if (resultToApply === undefined || resultToApply === null) {
                    throw new Error("No result to apply.");
                }

                if (this.replaceText) {
                    this.editor.replaceSelection(resultToApply);
                } else {
                    const end = this.editor.getCursor("to");
                    this.editor.replaceRange(resultToApply, end);
                }

                new Notice("Changes successfully applied!");
                this.close();
            } catch (err) {
                console.error("YAHA Apply error:", err);
                new Notice(`Failed to apply changes: ${err.message}`);
            }
        });
    }

/**
     * Reads the current CSS translate values of an element.
     */
    getTranslate(el) {
        const style = window.getComputedStyle(el).transform;
        if (!style || style === "none") return { x: 0, y: 0 };
        const m = style.match(/matrix\(([^)]+)\)/);
        if (!m) return { x: 0, y: 0 };
        const parts = m[1].split(", ").map(Number);
        return { x: parts[4] || 0, y: parts[5] || 0 };
    }

    /**
     * Makes the Obsidian modal draggable (by the title) and adds a manual
     * bottom-right resize handle. We move the modal with CSS translate so we
     * do not fight Obsidian's flex centering or transformed ancestors.
     */
    setupModalDragResize() {
        const modalEl = this.modalEl;
        if (!modalEl) return;

        modalEl.classList.add("yaha-draggable-modal");
        modalEl.style.maxWidth = "none";
        modalEl.style.maxHeight = "none";

        // --- Drag by title ---
        const titleEl = this.contentEl.querySelector(".yaha-modal-title");
        if (titleEl) {
            titleEl.classList.add("yaha-drag-handle");
            this.attachDragHandler(titleEl, modalEl);
        }

        // --- Manual resize handle ---
        this.attachResizeHandle(modalEl);
    }

    /**
     * Attaches pointer-based dragging to the given handle element.
     */
    attachDragHandler(handleEl, modalEl) {
        let isDragging = false;
        let startX = 0;
        let startY = 0;
        let startTx = 0;
        let startTy = 0;

        const onPointerMove = (e) => {
            if (!isDragging) return;
            const dx = e.clientX - startX;
            const dy = e.clientY - startY;
            modalEl.style.transform = `translate(${startTx + dx}px, ${startTy + dy}px)`;
        };

        const onPointerUp = () => {
            isDragging = false;
            window.removeEventListener("pointermove", onPointerMove);
            window.removeEventListener("pointerup", onPointerUp);
            modalEl.classList.remove("yaha-is-dragging");
        };

        handleEl.addEventListener("pointerdown", (e) => {
            if (e.button !== 0) return;
            isDragging = true;
            startX = e.clientX;
            startY = e.clientY;
            const t = this.getTranslate(modalEl);
            startTx = t.x;
            startTy = t.y;

            modalEl.classList.add("yaha-is-dragging");
            window.addEventListener("pointermove", onPointerMove);
            window.addEventListener("pointerup", onPointerUp);
            e.preventDefault();
        });

        if (typeof this.register === "function") {
            this.register(() => {
                window.removeEventListener("pointermove", onPointerMove);
                window.removeEventListener("pointerup", onPointerUp);
            });
        }
    }

    /**
     * Adds a bottom-right resize handle and resizes the modal via pointer events.
     */
    attachResizeHandle(modalEl) {
        const handle = document.createElement("div");
        handle.className = "yaha-resize-handle";
        handle.setAttribute("aria-label", "Resize dialog");
        modalEl.appendChild(handle);

        let isResizing = false;
        let startX = 0;
        let startY = 0;
        let startWidth = 0;
        let startHeight = 0;

        const onPointerMove = (e) => {
            if (!isResizing) return;
            const dx = e.clientX - startX;
            const dy = e.clientY - startY;
            modalEl.style.width = `${Math.max(320, startWidth + dx)}px`;
            modalEl.style.height = `${Math.max(240, startHeight + dy)}px`;
        };

        const onPointerUp = () => {
            isResizing = false;
            window.removeEventListener("pointermove", onPointerMove);
            window.removeEventListener("pointerup", onPointerUp);
            modalEl.classList.remove("yaha-is-resizing");
        };

        handle.addEventListener("pointerdown", (e) => {
            if (e.button !== 0) return;
            isResizing = true;
            startX = e.clientX;
            startY = e.clientY;
            startWidth = modalEl.offsetWidth;
            startHeight = modalEl.offsetHeight;
            modalEl.style.width = `${startWidth}px`;
            modalEl.style.height = `${startHeight}px`;

            modalEl.classList.add("yaha-is-resizing");
            window.addEventListener("pointermove", onPointerMove);
            window.addEventListener("pointerup", onPointerUp);
            e.preventDefault();
        });

        if (typeof this.register === "function") {
            this.register(() => {
                window.removeEventListener("pointermove", onPointerMove);
                window.removeEventListener("pointerup", onPointerUp);
                try {
                    handle.remove();
                } catch (e) {}
            });
        }
    }

    /**
     * Stops Obsidian's default behaviour of closing the modal when the
     * dark backdrop outside the modal is clicked.
     */
    preventBackdropClose() {
        const containerEl = this.containerEl;
        if (!containerEl) return;

        const bg = containerEl.querySelector(".modal-bg");
        if (!bg) return;

        const blockClick = (e) => {
            e.stopPropagation();
            e.preventDefault();
        };

        // Use capture phase so this runs before Obsidian's own click handler
        bg.addEventListener("click", blockClick, true);

        if (typeof this.register === "function") {
            this.register(() => {
                bg.removeEventListener("click", blockClick, true);
            });
        }
    }

    onClose() {
        this.cleanupProcess();
        const { contentEl } = this;
        contentEl.empty();
    }

    cleanupProcess() {
        if (this.processRef) {
            try {
                this.processRef.kill();
            } catch (e) {}
            this.processRef = null;
        }
    }

    async loadAgentsFromWorkspace() {
        try {
            const adapter = this.app.vault.adapter;
            const configPath = ".yaha/AgentClass.md";
            if (await adapter.exists(configPath)) {
                const content = await adapter.read(configPath);
                const foundAgents = [];
                const lines = content.split("\n");
                for (let line of lines) {
                    if (line.startsWith("## ")) {
                        const name = line.substring(3).trim();
                        if (name) foundAgents.push(name);
                    }
                }
                if (foundAgents.length > 0) {
                    this.agents = foundAgents;
                    // Auto-select first or BaseAgent if present
                    if (this.agents.includes("BaseAgent")) {
                        this.selectedAgent = "BaseAgent";
                    } else {
                        this.selectedAgent = this.agents[0];
                    }
                }
            }
        } catch (e) {
            console.error("Could not parse .yaha/AgentClass.md:", e);
        }
    }

    async executeYahaTask(instructions) {
        // Show console
        this.consoleContainer.style.display = "flex";
        this.consoleArea.textContent = "Starting YAHA engine...\n";
        this.executeBtn.innerHTML = '<span class="yaha-spinner"></span>Running...';

        try {
            const yahaDir = path.join(this.vaultPath, ".yaha");
            
            // Create temp files to feed instructions and capture outputs
            const promptFilePath = path.join(yahaDir, "coauthor_prompt.txt");
            const resultFilePath = path.join(yahaDir, "coauthor_result.txt");

            // The instruction box already contains the selected text (and any
            // edits the user made), so we send it as the full prompt.
            let combinedPrompt = instructions;

            fs.writeFileSync(promptFilePath, combinedPrompt, "utf-8");

            // Delete previous result file if exists
            if (fs.existsSync(resultFilePath)) {
                fs.unlinkSync(resultFilePath);
            }

            // Prepare environment
            const env = await this.prepareEnv();

            // Determine command binary path
            let commandBin = "yaha";
            if (this.plugin.settings.customYahaPath) {
                if (fs.existsSync(this.plugin.settings.customYahaPath)) {
                    commandBin = this.plugin.settings.customYahaPath;
                    this.consoleArea.textContent += `Using custom YAHA binary: ${commandBin}\n`;
                } else {
                    this.consoleArea.textContent += `Warning: Custom YAHA binary not found, falling back to 'yaha'\n`;
                }
            }

            // If we resolved an absolute path to the binary, prepend its directory to PATH
            if (path.isAbsolute(commandBin)) {
                const isWindows = process.platform === "win32";
                const pathSep = isWindows ? ";" : ":";
                const binDir = path.dirname(commandBin);
                const currentPath = env.PATH || "";
                env.PATH = binDir + pathSep + currentPath;
            }

            const cmdInfo = this.buildCommand(commandBin);
            this.consoleArea.textContent += `Executing command: ${cmdInfo.shellCommand}\n`;

            this.processRef = child_process.spawn(
                cmdInfo.useLoginShell ? cmdInfo.shellBin : commandBin,
                cmdInfo.useLoginShell ? cmdInfo.shellArgs : cmdInfo.primaryArgs,
                {
                    cwd: this.vaultPath,
                    env: env,
                    stdio: ["ignore", "pipe", "pipe"] // Connect stdin to /dev/null to prevent Python from blocking on sys.stdin.read()!
                }
            );
            this.processRef.stdout.on("data", (data) => {
                const str = data.toString();
                this.consoleArea.textContent += str;
                this.consoleArea.scrollTop = this.consoleArea.scrollHeight;
            });

            this.processRef.stderr.on("data", (data) => {
                const str = data.toString();
                this.consoleArea.textContent += `[Stderr] ${str}`;
                this.consoleArea.scrollTop = this.consoleArea.scrollHeight;
            });

            this.processRef.on("error", (err) => {
                console.error("YAHA launch error:", err);
                this.consoleArea.textContent += `\nError launching YAHA: ${err.message}\n`;
                if (err.code === "ENOENT") {
                    this.consoleArea.textContent += `Please make sure that the 'yaha' command is installed and available in your active environment, or configure your Python Virtual Environment path in the plugin settings.\n`;
                }
                
                // Restore UI state
                this.executeBtn.disabled = false;
                this.executeBtn.textContent = "Error";
                this.instrText.disabled = false;
                this.agentSelect.disabled = false;
                this.replaceCheckbox.disabled = false;
                this.viewDiffCheckbox.disabled = false;
                
                new Notice(`Launch error: ${err.message}`);
            });

            this.processRef.on("close", (code) => {
                this.processRef = null;

                // Clean up prompt file
                try { fs.unlinkSync(promptFilePath); } catch (e) {}

                if (code !== 0) {
                    this.consoleArea.textContent += `\nProcess exited with error code ${code}.\n`;
                    
                    // Restore UI state
                    this.executeBtn.disabled = false;
                    this.executeBtn.textContent = "Failed";
                    this.instrText.disabled = false;
                    this.agentSelect.disabled = false;
                    this.replaceCheckbox.disabled = false;
                    this.viewDiffCheckbox.disabled = false;
                    
                    new Notice("YAHA task execution failed.");
                    return;
                }

                // Verify result file
                if (!fs.existsSync(resultFilePath)) {
                    this.consoleArea.textContent += `\nError: Result file was not generated.\n`;
                    
                    // Restore UI state
                    this.executeBtn.disabled = false;
                    this.executeBtn.textContent = "No Output";
                    this.instrText.disabled = false;
                    this.agentSelect.disabled = false;
                    this.replaceCheckbox.disabled = false;
                    this.viewDiffCheckbox.disabled = false;
                    
                    new Notice("Agent finished but returned no response.");
                    return;
                }

                const resultText = fs.readFileSync(resultFilePath, "utf-8").trim();
                // Clean up result file
                try { fs.unlinkSync(resultFilePath); } catch (e) {}

                this.consoleArea.textContent += `\nSuccess! Result loaded.\n`;
                
                // Show completed controls
                this.showCompletionUI(resultText);
            });

        } catch (err) {
            this.consoleArea.textContent += `\nLauncher Error: ${err.message}\n`;
            this.executeBtn.textContent = "Launcher Error";
            new Notice(`Failed to launch YAHA: ${err.message}`);
        }
    }

    showCompletionUI(resultText) {
        const cleanResult = this.stripReasoningBlocks(resultText);
        this.resultText = cleanResult;

        // Swap Execute for Apply
        this.executeBtn.style.display = "none";
        this.applyBtn.style.display = "inline-block";

        // Hide console
        this.consoleContainer.style.display = "none";

        // Show final result in the pre-created result box
        if (this.resultArea) {
            this.resultArea.value = cleanResult;
        }

        // Re-enable checkboxes so the user can change options before applying
        if (this.replaceCheckbox) this.replaceCheckbox.disabled = false;
        if (this.viewDiffCheckbox) this.viewDiffCheckbox.disabled = false;

        // Always render the diff so toggling "View diff" works after completion.
        try {
            this.renderDiff(String(this.selection || ""), cleanResult);
            this.diffContainer.style.display = this.showDiff ? "block" : "none";
        } catch (err) {
            console.error("YAHA renderDiff error:", err);
            this.diffBody.innerHTML = "";
            this.diffBody.createEl("div", { text: `Could not render diff: ${err.message}`, cls: "yaha-diff-line yaha-diff-del" });
            this.diffContainer.style.display = "block";
        }
    }

    /**
     * Re-renders the diff against the current contents of the result box.
     */
    updateDiff() {
        if (!this.diffBody || !this.diffContainer) return;
        const currentResult = this.resultArea ? this.resultArea.value : (this.resultText || "");
        try {
            this.renderDiff(String(this.selection || ""), currentResult);
            this.diffContainer.style.display = this.showDiff ? "block" : "none";
        } catch (err) {
            console.error("YAHA renderDiff error:", err);
            this.diffBody.innerHTML = "";
            this.diffBody.createEl("div", { text: `Could not render diff: ${err.message}`, cls: "yaha-diff-line yaha-diff-del" });
            this.diffContainer.style.display = "block";
        }
    }

    /**
     * Strips DeepSeek-style <reasoning>...</reasoning> blocks from the result.
     */
    stripReasoningBlocks(text) {
        if (typeof text !== "string") return "";
        return text.replace(/<reasoning>[\s\S]*?<\/reasoning>\n*/g, "").trim();
    }

    renderDiff(original, edited) {
        if (!this.diffBody) {
            throw new Error("diffBody element is missing");
        }
        this.diffBody.innerHTML = "";

        original = String(original || "");
        edited = String(edited || "");

        const origLines = original.split("\n");
        const editLines = edited.split("\n");

        // Edge cases
        if (original === "" && edited === "") {
            this.diffBody.createEl("div", { text: "(no content to diff)", cls: "yaha-diff-line yaha-diff-unchanged" });
            return;
        }

        // LCS computation matrix
        const m = origLines.length;
        const n = editLines.length;
        const lcs = Array(m + 1).fill(null).map(() => Array(n + 1).fill(0));

        for (let i = 1; i <= m; i++) {
            for (let j = 1; j <= n; j++) {
                if (origLines[i - 1] === editLines[j - 1]) {
                    lcs[i][j] = lcs[i - 1][j - 1] + 1;
                } else {
                    lcs[i][j] = Math.max(lcs[i - 1][j], lcs[i][j - 1]);
                }
            }
        }

        // Backtrack to assemble diff line elements
        let i = m;
        let j = n;
        const diffList = [];

        while (i > 0 || j > 0) {
            if (i > 0 && j > 0 && origLines[i - 1] === editLines[j - 1]) {
                diffList.push({ type: "unchanged", text: origLines[i - 1] });
                i--;
                j--;
            } else if (j > 0 && (i === 0 || lcs[i][j - 1] >= lcs[i - 1][j])) {
                diffList.push({ type: "added", text: editLines[j - 1] });
                j--;
            } else {
                diffList.push({ type: "deleted", text: origLines[i - 1] });
                i--;
            }
        }

        diffList.reverse();

        if (diffList.length === 0) {
            this.diffBody.createEl("div", { text: "(no differences)", cls: "yaha-diff-line yaha-diff-unchanged" });
            return;
        }

        // Build HTML representation, collapsing long runs of unchanged lines.
        const renderLine = (line) => {
            let cls = "yaha-diff-line";
            let prefix = "  ";
            if (line.type === "added") {
                cls += " yaha-diff-add";
                prefix = "+ ";
            } else if (line.type === "deleted") {
                cls += " yaha-diff-del";
                prefix = "- ";
            } else {
                cls += " yaha-diff-unchanged";
            }
            const div = this.diffBody.createEl("div", { cls: cls });
            div.textContent = prefix + line.text;
        };

        let idx = 0;
        while (idx < diffList.length) {
            if (diffList[idx].type === "unchanged") {
                let runStart = idx;
                while (idx < diffList.length && diffList[idx].type === "unchanged") {
                    idx++;
                }
                const runLen = idx - runStart;
                if (runLen <= 3) {
                    for (let k = runStart; k < idx; k++) renderLine(diffList[k]);
                } else {
                    renderLine(diffList[runStart]);
                    this.diffBody.createEl("div", {
                        text: `... ${runLen - 2} unchanged lines ...`,
                        cls: "yaha-diff-line yaha-diff-unchanged"
                    });
                    renderLine(diffList[idx - 1]);
                }
            } else {
                renderLine(diffList[idx]);
                idx++;
            }
        }
    }
}

module.exports = YahaAgentsPlugin;
