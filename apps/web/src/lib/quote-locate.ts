/**
 * 인용 발췌(청크 전문)를 렌더된 소스 원문에서 찾아 해당 블록 요소를 돌려준다(ui-ux-improvements ②).
 *
 * 청크 텍스트는 원문 markdown과 정확히 같지 않다(합성 구분자, 제목 접두, markdown 기호). 그래서 글자·숫자만
 * 남겨 비교한다: 공백과 markdown·문장 기호를 모두 빼고 소문자로 맞춘 뒤, 인용의 앞·가운데·뒤 조각을 차례로
 * 찾는다. 정확한 원문 좌표는 코어가 청크 spans를 열면 그것으로 바꾼다.
 */

/** 비교에서 빼는 글자 — 공백과 markdown·표·문장 기호. */
const IGNORED = /[\s#*_`>|~[\]()!\-:=+.,;'"“”‘’·…/\\]/u;
/** 블록 하이라이트 단위. */
const BLOCK_SELECTOR = "p,li,h1,h2,h3,h4,h5,h6,td,th,pre,blockquote";
/** 찾을 조각 길이(정규화 글자 수). */
const SEGMENT = 40;
/** 이보다 짧은 인용은 오탐이 많아 찾지 않는다. */
const MIN_LENGTH = 6;

function normalize(text: string): string {
  let out = "";
  for (const ch of text) {
    if (!IGNORED.test(ch)) out += ch.toLowerCase();
  }
  return out;
}

export function locateQuote(root: HTMLElement, quote: string): Element[] | null {
  const needle = normalize(quote);
  if (needle.length < MIN_LENGTH) return null;

  // 문서 텍스트를 정규화하면서 글자마다 원래 텍스트 노드를 기억한다
  const owners: Text[] = [];
  let haystack = "";
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node as Text;
    for (const ch of text.data) {
      if (IGNORED.test(ch)) continue;
      haystack += ch.toLowerCase();
      owners.push(text);
    }
  }

  const seg = Math.min(SEGMENT, needle.length);
  const offsets = Array.from(
    new Set([0, Math.floor((needle.length - seg) / 2), needle.length - seg]),
  );
  for (const offset of offsets) {
    const at = haystack.indexOf(needle.slice(offset, offset + seg));
    if (at < 0) continue;
    const start = Math.max(0, at - offset);
    const end = Math.min(owners.length, start + needle.length);
    const blocks: Element[] = [];
    for (let i = start; i < end; i++) {
      const parent = owners[i].parentElement;
      const block = parent?.closest(BLOCK_SELECTOR) ?? parent;
      if (block && root.contains(block) && !blocks.includes(block)) blocks.push(block);
    }
    return blocks.length > 0 ? blocks : null;
  }
  return null;
}
