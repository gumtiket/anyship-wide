import { createLowlight } from 'lowlight';
import type { RootContent } from 'hast';
import bash from 'highlight.js/lib/languages/bash';
import css from 'highlight.js/lib/languages/css';
import dockerfile from 'highlight.js/lib/languages/dockerfile';
import ini from 'highlight.js/lib/languages/ini';
import javascript from 'highlight.js/lib/languages/javascript';
import json from 'highlight.js/lib/languages/json';
import python from 'highlight.js/lib/languages/python';
import typescript from 'highlight.js/lib/languages/typescript';
import xml from 'highlight.js/lib/languages/xml';
import yaml from 'highlight.js/lib/languages/yaml';
import type { FileDiff } from './unifiedDiff';

const highlighter = createLowlight({
  bash,
  css,
  dockerfile,
  ini,
  javascript,
  json,
  python,
  typescript,
  xml,
  yaml,
});
const extensions: Record<string, string> = {
  py: 'python',
  pyw: 'python',
  pyi: 'python',
  js: 'javascript',
  jsx: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  mts: 'typescript',
  cts: 'typescript',
  json: 'json',
  jsonc: 'json',
  yml: 'yaml',
  yaml: 'yaml',
  html: 'xml',
  htm: 'xml',
  xml: 'xml',
  svg: 'xml',
  css: 'css',
  sh: 'bash',
  bash: 'bash',
  zsh: 'bash',
  env: 'bash',
  ini: 'ini',
  toml: 'ini',
  dockerfile: 'dockerfile',
};

export type CodeToken = { text: string; className: string };
export type HighlightedDiff = Array<CodeToken[] | undefined>;

export function diffLanguage(path: string): string | undefined {
  const name = path.split('/').at(-1)!.toLowerCase();
  if (/^dockerfile(?:\.|$)/.test(name)) return 'dockerfile';
  if (name === '.env' || name.startsWith('.env.')) return 'bash';
  const extension = name.includes('.') ? name.split('.').at(-1)! : '';
  return Object.hasOwn(extensions, extension) ? extensions[extension] : undefined;
}

function highlightLines(language: string, code: string): CodeToken[][] {
  const tree = highlighter.highlight(language, code);
  const lines: CodeToken[][] = [[]];
  function visit(node: RootContent, classes: string[] = []) {
    if (node.type === 'element') {
      const ownClasses = node.properties.className;
      const nested = Array.isArray(ownClasses) ? [...classes, ...ownClasses.map(String)] : classes;
      node.children.forEach((child) => visit(child, nested));
    } else if (node.type === 'text') {
      const parts = node.value.split('\n');
      parts.forEach((text, index) => {
        if (index) lines.push([]);
        if (text) lines[lines.length - 1].push({ text, className: classes.join(' ') });
      });
    }
  }
  tree.children.forEach((node) => visit(node));
  return lines;
}

export function highlightDiff(file: FileDiff): HighlightedDiff | null {
  const language = diffLanguage(file.path);
  // 강조는 선택 사항이다. 크거나 압축된 패치는 응답성과 가독성을 위해 강조하지 않는다.
  if (
    !language ||
    file.lines.length > 1000 ||
    file.lines.some((line) => line.text.length > 2000) ||
    file.lines.reduce((size, line) => size + line.text.length, 0) > 50_000
  )
    return null;
  const result: HighlightedDiff = new Array(file.lines.length);
  let previous: number[] = [];
  let next: number[] = [];
  function flush() {
    // 삭제된 문자열·주석의 상태가 새 코드로 번지지 않도록 두 버전을 따로 강조한다.
    // 공통 문맥 줄은 새 버전의 토큰 색을 쓴다.
    for (const indices of [previous, next]) {
      if (!indices.length) continue;
      const source = indices.map((index) => file.lines[index].text);
      const tokens = highlightLines(language!, source.join('\n'));
      if (
        tokens.length !== source.length ||
        tokens.some((line, index) => line.map((token) => token.text).join('') !== source[index])
      ) {
        throw new Error('Highlighting changed source text');
      }
      indices.forEach((index, offset) => {
        result[index] = tokens[offset];
      });
    }
    previous = [];
    next = [];
  }
  try {
    file.lines.forEach((line, index) => {
      // diff는 hunk 사이의 코드를 생략하므로, 빈 구간을 건너 어휘 상태를 이어 가지 않는다.
      if (line.kind === 'hunk') flush();
      if (line.kind === 'context' || line.kind === 'deletion') previous.push(index);
      if (line.kind === 'context' || line.kind === 'addition') next.push(index);
    });
    flush();
    return result;
  } catch {
    return null;
  }
}
