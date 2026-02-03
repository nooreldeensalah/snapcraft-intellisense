import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';

// Documentation base URLs
const DOCS_BASE = 'https://documentation.ubuntu.com/snapcraft/stable';
const DOCS_REFERENCE = `${DOCS_BASE}/reference/project-file/snapcraft-yaml/`;
const DOCS_PLUGINS = `${DOCS_BASE}/reference/plugins/`;
const DOCS_BASES = `${DOCS_BASE}/reference/bases/`;
const INTERFACES_DOCS = 'https://snapcraft.io/docs/supported-interfaces';

// Cached schema data
let schemaInterfaces: Set<string> | null = null;
let schemaPlugins: Set<string> | null = null;
let schemaBases: Set<string> | null = null;

function isHoverEnabled(): boolean {
  return vscode.workspace.getConfiguration('snapcraft').get<boolean>('hover.enable', true);
}

/**
 * Load dynamic data from the generated schema file.
 */
function loadSchemaData(context: vscode.ExtensionContext): void {
  try {
    const schemaPath = path.join(context.extensionPath, 'schemas', 'snapcraft.json');
    const schemaContent = fs.readFileSync(schemaPath, 'utf8');
    const schema = JSON.parse(schemaContent);

    // Extract interface names from slots propertyNames enum
    const slotsEnum = schema.properties?.slots?.propertyNames?.enum;
    if (Array.isArray(slotsEnum)) {
      schemaInterfaces = new Set(slotsEnum);
      console.log(`Loaded ${schemaInterfaces.size} interfaces from schema`);
    } else {
      schemaInterfaces = new Set();
    }

    // Extract plugin names from Part definition
    const pluginEnum = schema.$defs?.Part?.properties?.plugin?.enum;
    if (Array.isArray(pluginEnum)) {
      schemaPlugins = new Set(pluginEnum);
      console.log(`Loaded ${schemaPlugins.size} plugins from schema`);
    } else {
      schemaPlugins = new Set();
    }

    // Extract base names from base property enum
    const baseEnum = schema.properties?.base?.enum;
    if (Array.isArray(baseEnum)) {
      schemaBases = new Set(baseEnum);
      console.log(`Loaded ${schemaBases.size} bases from schema`);
    } else {
      schemaBases = new Set();
    }
  } catch (error) {
    console.error('Failed to load schema data:', error);
    schemaInterfaces = new Set();
    schemaPlugins = new Set();
    schemaBases = new Set();
  }
}

/**
 * Build documentation URL for a property key.
 * URLs follow the pattern: base-url#property-name
 */
function getPropertyDocUrl(key: string): string {
  // Properties use lowercase with hyphens as anchors
  return `${DOCS_REFERENCE}#${key}`;
}

/**
 * Build plugin documentation URL.
 * Plugins use the pattern: plugin-name-plugin/
 */
function getPluginDocUrl(plugin: string): string | null {
  // Handle special cases
  if (plugin === '.net' || plugin === 'dotnet') {
    return `${DOCS_PLUGINS}dotnet-plugin/`;
  }

  // Most plugins follow: plugin-name-plugin/
  return `${DOCS_PLUGINS}${plugin}-plugin/`;
}

/**
 * Build base documentation URL.
 */
function getBaseDocUrl(base: string): string {
  return `${DOCS_BASES}#${base}`;
}

export function activate(context: vscode.ExtensionContext) {
  console.log('Snapcraft YAML extension is now active');

  // Load schema data for hover providers
  loadSchemaData(context);

  // Register hover provider for enhanced documentation links
  const hoverProvider = vscode.languages.registerHoverProvider(
    { language: 'yaml', pattern: '**/snapcraft.{yml,yaml}' },
    new SnapcraftHoverProvider()
  );
  context.subscriptions.push(hoverProvider);

  // Register code actions for quick fixes
  const codeActionProvider = vscode.languages.registerCodeActionsProvider(
    { language: 'yaml', pattern: '**/snapcraft.{yml,yaml}' },
    new SnapcraftCodeActionProvider(),
    { providedCodeActionKinds: [vscode.CodeActionKind.QuickFix] }
  );
  context.subscriptions.push(codeActionProvider);

  // Register commands
  context.subscriptions.push(
    vscode.commands.registerCommand('snapcraft.showDocumentation', () => showDocumentation()),
    vscode.commands.registerCommand('snapcraft.openDocs', (key: string) => openDocs(key))
  );

  // Show welcome message on first activation
  const hasShownWelcome = context.globalState.get('snapcraft.hasShownWelcome');
  if (!hasShownWelcome) {
    vscode.window.showInformationMessage(
      'Snapcraft YAML extension activated! IntelliSense and schema validation are now available for snapcraft.yaml and snapcraft.yml files.',
      'View Documentation'
    ).then(selection => {
      if (selection === 'View Documentation') {
        vscode.env.openExternal(vscode.Uri.parse(DOCS_REFERENCE));
      }
    });
    context.globalState.update('snapcraft.hasShownWelcome', true);
  }
}

export function deactivate() {
  // Cleanup if needed
}

/**
 * Hover provider for enhanced documentation links.
 * The JSON schema provides basic descriptions; this adds clickable doc links.
 */
class SnapcraftHoverProvider implements vscode.HoverProvider {
  provideHover(
    document: vscode.TextDocument,
    position: vscode.Position,
    _token: vscode.CancellationToken
  ): vscode.Hover | null {
    if (!isHoverEnabled()) return null;

    const wordRange = document.getWordRangeAtPosition(position, /[a-zA-Z0-9_-]+/);
    if (!wordRange) return null;

    const word = document.getText(wordRange);
    const lineText = document.lineAt(position.line).text;

    // Check if this is a key or value
    const colonIndex = lineText.indexOf(':');
    const isKey = colonIndex === -1 || position.character < colonIndex;

    if (isKey) {
      // Provide documentation link for top-level keys
      const docUrl = getPropertyDocUrl(word);
      const content = new vscode.MarkdownString();
      content.appendMarkdown(`**${word}**\n\n`);
      content.appendMarkdown(`[View Documentation](${docUrl})`);
      content.isTrusted = true;
      return new vscode.Hover(content, wordRange);
    } else {
      // Provide documentation for values
      const key = lineText.substring(0, colonIndex).trim();

      // Plugin documentation (loaded from schema)
      if (key === 'plugin' && schemaPlugins && schemaPlugins.has(word)) {
        const pluginUrl = getPluginDocUrl(word);
        if (pluginUrl) {
          const content = new vscode.MarkdownString();
          content.appendMarkdown(`**${word}** plugin\n\n`);
          content.appendMarkdown(`[View Plugin Documentation](${pluginUrl})`);
          content.isTrusted = true;
          return new vscode.Hover(content, wordRange);
        }
      }

      // Base snap documentation (loaded from schema)
      if ((key === 'base' || key === 'build-base') && schemaBases && schemaBases.has(word)) {
        const content = new vscode.MarkdownString();
        content.appendMarkdown(`**${word}** base snap\n\n`);
        content.appendMarkdown(`[View Base Documentation](${getBaseDocUrl(word)})`);
        content.isTrusted = true;
        return new vscode.Hover(content, wordRange);
      }

      // Interface documentation (loaded from schema) - THE KILLER FEATURE
      if (schemaInterfaces && schemaInterfaces.has(word)) {
        const context = this.getInterfaceContext(document, position);
        if (context.isInPlugsOrSlots) {
          const content = new vscode.MarkdownString();
          content.appendMarkdown(`**${word}** interface\n\n`);
          content.appendMarkdown(`Part of Snapcraft's interface system for controlled access to system resources.\n\n`);
          content.appendMarkdown(`[View All Supported Interfaces](${INTERFACES_DOCS})`);
          content.isTrusted = true;
          return new vscode.Hover(content, wordRange);
        }
      }
    }

    return null;
  }

  /**
   * Determine if the current position is within a plugs or slots section.
   */
  private getInterfaceContext(document: vscode.TextDocument, position: vscode.Position): { isInPlugsOrSlots: boolean } {
    const currentLine = position.line;
    let isInPlugsOrSlots = false;

    // Look backwards for plugs: or slots: key at the same or lower indentation level
    for (let i = currentLine; i >= Math.max(0, currentLine - 50); i--) {
      const line = document.lineAt(i).text;
      const match = line.match(/^(\s*)(plugs|slots):/);

      if (match) {
        const keyIndent = match[1].length;
        const currentIndent = document.lineAt(currentLine).text.match(/^(\s*)/)?.[1].length ?? 0;

        // Check if we're at a deeper indentation (inside the section)
        if (currentIndent > keyIndent) {
          isInPlugsOrSlots = true;
        }
        break;
      }
    }

    return { isInPlugsOrSlots };
  }
}

/**
 * Code action provider for quick fixes.
 */
class SnapcraftCodeActionProvider implements vscode.CodeActionProvider {
  provideCodeActions(
    _document: vscode.TextDocument,
    _range: vscode.Range,
    _context: vscode.CodeActionContext,
    _token: vscode.CancellationToken
  ): vscode.CodeAction[] {
    // No custom code actions needed - schema validation handles everything
    return [];
  }
}

async function showDocumentation(): Promise<void> {
  const choice = await vscode.window.showQuickPick([
    { label: 'Snapcraft YAML Reference', url: DOCS_REFERENCE },
    { label: 'Plugins', url: DOCS_PLUGINS },
    { label: 'Interfaces', url: INTERFACES_DOCS },
    { label: 'Extensions', url: `${DOCS_BASE}/reference/extensions/` },
    { label: 'Bases', url: DOCS_BASES },
    { label: 'Layouts', url: `${DOCS_BASE}/reference/layouts/` },
    { label: 'Hooks', url: `${DOCS_BASE}/reference/hooks/` },
    { label: 'Package Repositories', url: `${DOCS_BASE}/reference/package-repositories/` },
    { label: 'Components', url: `${DOCS_BASE}/reference/components/` },
  ], {
    placeHolder: 'Select documentation to view'
  });

  if (choice) {
    vscode.env.openExternal(vscode.Uri.parse(choice.url));
  }
}

function openDocs(key: string): void {
  const url = getPropertyDocUrl(key);
  vscode.env.openExternal(vscode.Uri.parse(url));
}
