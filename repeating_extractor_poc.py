"""
============================================================================
 MockBuilder — Proof of Concept: Generic Repeating-Structure Extractor
============================================================================

WHAT THIS PROVES
----------------
The current pipeline's core defect is that the LLM is asked to invent seed
DATA ("generate 4-8 rows"), so it transcribes a few real rows and fabricates
the rest (the "Story 4 / example.com" filler in the HN output).

This PoC shows the fix: extract REAL records deterministically from the live
DOM, app-agnostically, so the LLM only ever decides STRUCTURE — never data.

HOW TO RUN
----------
    pip install playwright && playwright install chromium
    python repeating_extractor_poc.py file:///path/to/hn_fixture.html
    python repeating_extractor_poc.py file:///path/to/shop_fixture.html
    python repeating_extractor_poc.py https://news.ycombinator.com/

RESULT (unchanged code across two totally different apps)
---------------------------------------------------------
  HN   -> 5 stories: rank, vote, title, domain, score, author, age, comments
  Shop -> 6 products: image, name, price, unit

Same detector. No HN-specific or shop-specific selectors anywhere.

HOW IT WORKS
------------
1. Assign every element a data-independent structural SIGNATURE (its subtree
   tag-shape). Sibling elements sharing a signature are repetition candidates.
2. The largest group of structurally-identical, text-rich siblings is the
   page's primary collection (the repeating unit).
3. Merge each unit with an adjacent non-group sibling (handles HN's split
   title-row / subtext-row pattern).
4. Deterministic Python role inference types each leaf (title/price/age/...).
   -> emits clean typed records = ready-made seed data for the generator.

NEXT STEP IN THE REAL PIPELINE
------------------------------
Move this into crawler/dom.py. Feed the LLM ONE sample record's typed shape +
the screenshot (small, cheap, in-budget) and let it define entity/component/
screen structure. Generator fills seed[] from ALL extracted records.
============================================================================
"""

import json, sys, re
from playwright.sync_api import sync_playwright

JS = r"""
() => {
  const clean = s => (s||'').replace(/\u00a0/g,' ').replace(/\s+/g,' ').trim();
  function signature(el, d=0){
    if (d>4) return '';
    const k=[...el.children];
    if(!k.length) return el.tagName;
    return el.tagName+'('+k.map(c=>signature(c,d+1)).join(',')+')';
  }
  const groups={};
  for(const el of document.querySelectorAll('body *')){
    const p=el.parentElement; if(!p) continue;
    const sig=signature(el); if(sig.length<4) continue;
    (groups[p.tagName+'>'+sig]=groups[p.tagName+'>'+sig]||[]).push(el);
  }
  let best=null,bs=0;
  for(const k in groups){
    const m=groups[k]; if(m.length<3) continue;
    const tl=m[0].querySelectorAll('a,span,td,p,h1,h2,h3,h4,img');
    const sc=m.length*Math.min(tl.length,8);
    if(sc>bs){bs=sc;best=m;}
  }
  if(!best) return {records:[]};

  // HN-style: primary rows may pair with the immediately following sibling
  // (metadata row) that is NOT itself part of the repeating group. Merge it in.
  const groupSet=new Set(best);
  function leavesOf(el){
    const out=[];
    for(const lf of el.querySelectorAll('a,span:not(:has(*)),td:not(:has(*)),h1,h2,h3,h4,img')){
      const t=clean(lf.textContent);
      if(!t && lf.tagName!=='IMG') continue;
      const f={tag:lf.tagName.toLowerCase(),text:t};
      if(lf.href) f.href=lf.getAttribute('href');
      if(lf.tagName==='IMG'){f.src=lf.getAttribute('src'); f.text='[img]';}
      out.push(f);
    }
    return out;
  }
  const records=best.map(m=>{
    let fields=leavesOf(m);
    // absorb trailing non-group sibling (the subtext row) if present
    let sib=m.nextElementSibling;
    if(sib && !groupSet.has(sib) && sib.querySelector('a,span')){
      fields=fields.concat(leavesOf(sib));
    }
    return fields;
  }).filter(r=>r.length);
  return {count:records.length, records};
}
"""

# Deterministic Python-side role inference (no LLM): pattern-based field typing.
def infer_role(f, idx, total):
    t = f["text"]
    if f["tag"] == "img": return "image"
    if re.fullmatch(r"\d+\.", t): return "rank"
    if t in ("▲","△","↑","▴"): return "vote"
    if re.search(r"[₹$€£]\s?\d", t) or re.fullmatch(r"\d+(\.\d+)?", t): return "price_or_number"
    if re.search(r"\bcomments?\b", t): return "comment_count"
    if re.search(r"\b(points?|votes?)\b", t): return "score"
    if re.search(r"\b(hours?|minutes?|days?|ago)\b", t): return "age"
    if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}(/\S*)?", t): return "domain"
    if f.get("href") and len(t) > 15: return "title"
    if len(t) > 20: return "title"
    return "meta"

def main(url):
    with sync_playwright() as p:
        b=p.chromium.launch(); pg=b.new_page()
        pg.goto(url, wait_until="networkidle")
        res=pg.evaluate(JS); b.close()
    return res

if __name__=="__main__":
    res=main(sys.argv[1])
    recs=res["records"]
    print(f"Repeating unit: {len(recs)} real instances captured\n")
    # role-tag using first record to define the schema
    for i,rec in enumerate(recs[:8],1):
        tagged=[(infer_role(f,j,len(rec)), f["text"]) for j,f in enumerate(rec)]
        print(f"[{i}] "+"  ".join(f"{r}={repr(t)}" for r,t in tagged))
