(() => {
  if (globalThis.__automadinCompanionInstalled) return;
  globalThis.__automadinCompanionInstalled = true;
  const snapshots = new Map();
  const text = el => (el.innerText || el.textContent || "").trim();
  const role = el => el.getAttribute("role") || ({BUTTON:"button",A:"link",SELECT:"combobox"}[el.tagName] || (el.tagName === "INPUT" ? "textbox" : ""));
  const name = el => (el.getAttribute("aria-label") || el.getAttribute("alt") || el.getAttribute("title") || text(el)).trim();
  function direct(spec, root=document) {
    const s = spec.strategy || "css";
    if (s === "css") return [...root.querySelectorAll(spec.value)];
    if (s === "xpath") { const r=document.evaluate(spec.value,root,null,XPathResult.ORDERED_NODE_SNAPSHOT_TYPE); return Array.from({length:r.snapshotLength},(_,i)=>r.snapshotItem(i)); }
    const all=[...root.querySelectorAll("*")]; const eq=(a,b)=>spec.exact?a===b:a.includes(b);
    if(s==="text")return all.filter(e=>eq(text(e),spec.value));
    if(s==="test_id")return all.filter(e=>e.dataset.testid===spec.value||e.dataset.test===spec.value);
    if(s==="label")return [...document.querySelectorAll("label")].filter(e=>eq(text(e),spec.value)).map(e=>e.control).filter(Boolean);
    if(s==="placeholder")return all.filter(e=>eq(e.getAttribute("placeholder")||"",spec.value));
    if(s==="alt_text")return all.filter(e=>eq(e.getAttribute("alt")||"",spec.value));
    if(s==="title")return all.filter(e=>eq(e.getAttribute("title")||"",spec.value));
    if(s==="role")return all.filter(e=>role(e)===spec.role&&(!spec.name||eq(name(e),spec.name)));
    throw new Error(`Unsupported locator strategy: ${s}`);
  }
  function locate(raw){const s=typeof raw==="string"?{strategy:"css",value:raw}:raw;if(s.strategy==="snapshot")return [snapshots.get(s.value)?.[Number(s.index)]].filter(Boolean);for(const c of [s,...(s.fallbacks||[])]){const m=direct(c);if(m.length)return m;}return [];}
  function first(raw){const e=locate(raw)[0];if(!e)throw new Error("Browser element was not found");return e;}
  function geometry(e){e.scrollIntoView({block:"center",inline:"center"});const r=e.getBoundingClientRect();if(!r.width||!r.height)throw new Error("Browser element is not visible");return{x:r.x,y:r.y,width:r.width,height:r.height,device_pixel_ratio:devicePixelRatio,viewport_width:innerWidth,viewport_height:innerHeight};}
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const editable = e => e instanceof HTMLInputElement || e instanceof HTMLTextAreaElement || e instanceof HTMLSelectElement || e.isContentEditable;
  function delay(p){const lo=Math.max(0,Number(p.min_delay||p.min_delay_ms||0));const hi=Math.max(lo,Number(p.max_delay||p.max_delay_ms||0));return hi===lo?lo:Math.floor(lo+Math.random()*(hi-lo+1));}
  function setValue(e,value,inputType="insertText",data=null){
    if(e instanceof HTMLSelectElement){e.value=value;e.dispatchEvent(new Event("input",{bubbles:true}));return;}
    if(e.isContentEditable){e.textContent=value;}
    else {
      const proto=e instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
      const setter=Object.getOwnPropertyDescriptor(proto,"value")?.set;
      if(!setter)throw new Error("Browser field value cannot be changed");
      setter.call(e,value);
    }
    e.dispatchEvent(new InputEvent("input",{bubbles:true,composed:true,inputType,data}));
  }
  function insertText(e,text){
    const current=String(e.value??e.textContent??"");
    if(typeof e.selectionStart==="number"&&typeof e.selectionEnd==="number"){
      const start=e.selectionStart,end=e.selectionEnd;
      setValue(e,current.slice(0,start)+text+current.slice(end),"insertText",text);
      e.setSelectionRange(start+text.length,start+text.length);
    } else setValue(e,current+text,"insertText",text);
  }
  async function enterText(e,value,p,{replace}){
    if(!editable(e))throw new Error("No editable browser element has focus");
    if(e.disabled||e.readOnly)throw new Error("Browser field is disabled or read-only");
    e.focus({preventScroll:true});
    if(replace)setValue(e,"","deleteContentBackward",null);
    const pause=delay(p);
    if(pause<=0){
      const before=new InputEvent("beforeinput",{bubbles:true,cancelable:true,composed:true,inputType:"insertText",data:value});
      if(e.dispatchEvent(before))insertText(e,value);
    }
    else for(const ch of value){
      const before=new InputEvent("beforeinput",{bubbles:true,cancelable:true,composed:true,inputType:"insertText",data:ch});
      if(!e.dispatchEvent(before))continue;
      insertText(e,ch);
      await wait(delay(p));
    }
    e.dispatchEvent(new Event("change",{bubbles:true}));
  }
  async function clickAt(e,p){
    const r=e.getBoundingClientRect();
    const factors={top_left:[0,0],top:[.5,0],top_right:[1,0],left:[0,.5],center:[.5,.5],right:[1,.5],bottom_left:[0,1],bottom:[.5,1],bottom_right:[1,1]};
    const [fx,fy]=factors[p.anchor]||factors.center;
    const ox=p.offset_unit==="percent"?Number(p.offset_x||0)*r.width/100:Number(p.offset_x||0);
    const oy=p.offset_unit==="percent"?Number(p.offset_y||0)*r.height/100:Number(p.offset_y||0);
    const x=Number.isFinite(Number(p.x))?Number(p.x):r.left+r.width*fx+ox;
    const y=Number.isFinite(Number(p.y))?Number(p.y):r.top+r.height*fy+oy;
    const target=document.elementFromPoint(x,y)||e;
    if(typeof target.focus==="function")target.focus({preventScroll:true});
    const init={bubbles:true,cancelable:true,composed:true,clientX:x,clientY:y,button:0,buttons:1};
    target.dispatchEvent(new PointerEvent("pointerdown",init));
    target.dispatchEvent(new MouseEvent("mousedown",init));
    if(Number(p.hold_duration_ms||0)>0)await wait(Number(p.hold_duration_ms));
    target.dispatchEvent(new PointerEvent("pointerup",{...init,buttons:0}));
    target.dispatchEvent(new MouseEvent("mouseup",{...init,buttons:0}));
    target.dispatchEvent(new MouseEvent("click",{...init,buttons:0,detail:1}));
    if(p.click_type==="double"){
      target.dispatchEvent(new MouseEvent("click",{...init,buttons:0,detail:2}));
      target.dispatchEvent(new MouseEvent("dblclick",{...init,buttons:0,detail:2}));
    }
  }
  const keyName = key => key === "Space" ? " " : String(key);
  function radioGroupKey(e){
    if(!(e instanceof HTMLInputElement)||e.type!=="radio"||!e.name)return null;
    return {form:e.form,name:e.name};
  }
  function focusable(){
    const candidates=[...document.querySelectorAll('a[href],button,input,select,textarea,[contenteditable="true"],[tabindex]')]
      .filter(e=>!e.disabled&&e.tabIndex>=0&&!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length));
    const radioGroups=[];
    for(const e of candidates){
      const key=radioGroupKey(e);if(!key)continue;
      let group=radioGroups.find(item=>item.form===key.form&&item.name===key.name);
      if(!group){group={...key,radios:[]};radioGroups.push(group);}
      group.radios.push(e);
    }
    const radioStops=new Set(radioGroups.map(group=>group.radios.find(e=>e.checked)||group.radios[0]));
    return candidates.filter(e=>radioGroupKey(e)===null||radioStops.has(e));
  }
  function moveFocus(backward=false){
    const items=focusable();if(!items.length)return;
    const index=items.indexOf(document.activeElement);
    const next=backward?(index<=0?items.length-1:index-1):(index<0||index===items.length-1?0:index+1);
    items[next].focus({preventScroll:true});
  }
  function deleteText(e,backward){
    if(!editable(e)||e instanceof HTMLSelectElement)return;
    const current=String(e.value??e.textContent??"");
    let start=typeof e.selectionStart==="number"?e.selectionStart:current.length;
    let end=typeof e.selectionEnd==="number"?e.selectionEnd:start;
    if(start===end){if(backward&&start>0)start--;else if(!backward&&end<current.length)end++;}
    setValue(e,current.slice(0,start)+current.slice(end),backward?"deleteContentBackward":"deleteContentForward",null);
    if(typeof e.setSelectionRange==="function")e.setSelectionRange(start,start);
  }
  async function pressOne(raw,modifiers={}){
    const key=keyName(raw),e=document.activeElement||document.body;
    const init={key,code:key.length===1?`Key${key.toUpperCase()}`:String(raw),bubbles:true,cancelable:true,composed:true,...modifiers};
    const proceed=e.dispatchEvent(new KeyboardEvent("keydown",init));
    if(proceed){
      if(raw==="Tab")moveFocus(Boolean(modifiers.shiftKey));
      else if(raw==="Enter"){
        if(typeof e.click==="function"&&e.matches('button,a[href],input[type="button"],input[type="submit"]'))e.click();
        else if(e.form&&typeof e.form.requestSubmit==="function")e.form.requestSubmit();
      } else if(raw==="Space"){
        if(typeof e.click==="function"&&e.matches('button,input[type="checkbox"],input[type="radio"]'))e.click();
        else if(editable(e))insertText(e," ");
      } else if(raw==="Backspace")deleteText(e,true);
      else if(raw==="Delete")deleteText(e,false);
      else if((modifiers.ctrlKey||modifiers.metaKey)&&String(raw).toLowerCase()==="a"&&typeof e.select==="function")e.select();
      else if(String(raw).length===1&&editable(e)&&!modifiers.ctrlKey&&!modifiers.metaKey&&!modifiers.altKey)insertText(e,String(raw));
    }
    e.dispatchEvent(new KeyboardEvent("keyup",init));
  }
  async function pressKeys(keys,p){
    const modifiers={ctrlKey:false,shiftKey:false,altKey:false,metaKey:false};
    const modifierNames={Control:"ctrlKey",Shift:"shiftKey",Alt:"altKey",Meta:"metaKey"};
    if(p.mode==="simultaneous"){
      for(const key of keys)if(modifierNames[key])modifiers[modifierNames[key]]=true;
      for(const key of keys)if(!modifierNames[key])await pressOne(key,modifiers);
      return;
    }
    for(let i=0;i<keys.length;i++){await pressOne(keys[i]);if(i<keys.length-1&&Number(p.pause_ms||0)>0)await wait(Number(p.pause_ms));}
  }
  chrome.runtime.onMessage.addListener((request,_sender,sendResponse)=>{(async()=>{try{const p=request.params||{};let result=null;
    if(request.op==="locate")result=geometry(first(p.locator));
    else if(request.op==="focus"){const e=first(p.locator);e.focus();result=geometry(e);}
    else if(request.op==="click"){const e=first(p.locator);geometry(e);await clickAt(e,p);}
    else if(request.op==="click_position"){const e=document.elementFromPoint(Number(p.x),Number(p.y));if(!e)throw new Error("No browser element exists at that position");await clickAt(e,p);}
    else if(request.op==="fill"){const e=first(p.locator);geometry(e);await enterText(e,String(p.value??""),p,{replace:true});}
    else if(request.op==="type"){await enterText(document.activeElement,String(p.text??""),p,{replace:false});}
    else if(request.op==="press"){await pressKeys(Array.isArray(p.keys)?p.keys.map(String):[],p);}
    else if(request.op==="visible"){const e=locate(p.locator)[0];result=!!e&&!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);}
    else if(request.op==="wait")result=!!locate(p.locator)[0];
    else if(request.op==="extract")result=text(first(p.locator));
    else if(request.op==="count")result=locate(p.locator).length;
    else if(request.op==="snapshot"){const token=crypto.randomUUID();const elements=locate(p.locator);snapshots.set(token,elements);result={token,count:elements.length};}
    else if(request.op==="snapshot_extract"){let e=snapshots.get(p.token)?.[Number(p.index)];if(!e)throw new Error("Element snapshot is stale");if(p.child_selector)e=e.querySelector(p.child_selector);if(!e)throw new Error("Snapshot child was not found");result=text(e);}
    else if(request.op==="snapshot_scroll"){const e=snapshots.get(p.token)?.[Number(p.index)];if(!e)throw new Error("Element snapshot is stale");e.scrollIntoView({block:"center"});}
    else if(request.op==="snapshot_release"){snapshots.delete(p.token);}
    else if(request.op==="scroll")first(p.locator).scrollIntoView({block:"center"});
    else if(request.op==="scroll_page")window.scrollBy(p.delta_x||0,p.delta_y||0);
    else if(request.op==="viewport")result={width:innerWidth,height:innerHeight};
    else throw new Error(`Unsupported companion operation: ${request.op}`);
    sendResponse({ok:true,result});}catch(error){sendResponse({ok:false,error:String(error?.message||error)});}})();return true;});
})();
