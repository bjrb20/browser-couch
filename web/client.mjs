import {inputMask,inputAxes,isControlKey} from './controls.mjs';
const $=id=>document.getElementById(id),video=$('video'),rtcAudio=$('rtc-audio');
let token=null,pc=null,channel=null,roomState=null,mine=-1,sequence=0,joining=false,focused=document.hasFocus(),polling=false;
let rtt=null,frames=0,lastFrames=0,streamFPS=0,lastStats=performance.now();
let socket=null,transport='webrtc',fallbackReady=false,latestJPEG=null,decoding=false,sampleRate=44100,audioContext=null,audioGain=null,audioAt=0;
let audioPackets=0,muted=true;
let lastSentMask=null,lastSentAt=0,receiverBufferMs=null,receivedToDisplayMs=null,previousJitter=null;
const keys=new Set(),touches=new Map(),sticks=[[0,0],[0,0]];
let analog=false,lastSentAxes='';
const incoming=new MediaStream(),incomingAudio=new MediaStream();
async function api(path,body){
  const response=await fetch('api/'+path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},...(body===undefined?{}:{body:JSON.stringify(body)})});
  if(!response.ok)throw new Error((await response.text()).slice(0,240));return response.json();
}
function message(text){$('message').textContent=text;}
function connected(text,active=false){$('connection').textContent=text;$('connection').classList.toggle('connected',active);}
function controlReady(){return channel?.readyState==='open'||(socket?.readyState===WebSocket.OPEN&&fallbackReady);}
function send(data){const target=transport==='websocket'?socket:channel;if(controlReady()&&target?.bufferedAmount<16384){target.send(JSON.stringify(data));return true;}return false;}
function sendInput(force=false){
  if(!controlReady())return;
  const active=mine>=0&&focused&&!document.hidden&&!editing();
  const pad=selectedPad(),mask=active?inputMask(keys,touches.values(),pad,analog):0,now=performance.now();
  const axes=active&&analog?inputAxes(keys,sticks,pad):[0,0,0,0],axesKey=axes.join();
  if((force||mask!==lastSentMask||axesKey!==lastSentAxes||now-lastSentAt>=100)&&send({t:'input',seq:++sequence,mask,...(analog?{axes}:{})})){
    lastSentMask=mask;lastSentAxes=axesKey;lastSentAt=now;
  }
}
function clearInput(){keys.clear();touches.clear();sticks.forEach((s,n)=>moveStick(n,0,0));lastSentAxes='0,0,0,0';document.querySelectorAll('.pressed').forEach(b=>b.classList.remove('pressed'));if(send({t:'input',seq:++sequence,mask:0})){lastSentMask=0;lastSentAt=performance.now();}}
function editing(){return /INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName)||!$('game-picker').hidden;}
function gamepads(){try{return Array.from(navigator.getGamepads?.()||[]).filter(Boolean);}catch{return [];}}
function selectedPad(){const pads=gamepads();return $('gamepad').value==='auto'?pads[0]:pads.find(p=>String(p.index)===$('gamepad').value);}
function updatePads(){
  const old=$('gamepad').value,pads=gamepads();
  $('gamepad').replaceChildren(new Option('Keyboard / first gamepad','auto'),...pads.map(p=>new Option(p.id.slice(0,48),String(p.index))));
  if([...$('gamepad').options].some(o=>o.value===old))$('gamepad').value=old;
  const pad=selectedPad();
  $('pad-note').textContent=!isSecureContext?'Gamepads need HTTPS on LAN. Keyboard and touch work here.'
    :!pad?'Connect a gamepad and press a button, or use your keyboard.'
    // Without the standard layout the browser gives raw button numbers, and they go to the wrong buttons.
    :pad.mapping!=='standard'?'This browser does not know the layout of this gamepad. Use Chrome, Edge or Safari.'
    :analog?'Gamepad connected. Its sticks are the analog sticks.':'Gamepad connected. Its left stick also acts as the D-pad.';
}
function renderSlots(){
  const slots=roomState?.slots||Array.from({length:4},(_,n)=>({number:n+1,name:null,yours:false}));
  mine=slots.findIndex(s=>s.yours);
  if(mine>=0)document.body.dataset.player=String(mine%4+1);else delete document.body.dataset.player;
  $('slots').replaceChildren(...slots.map((s,n)=>{
    const b=document.createElement('button');b.className='slot p'+(n%4+1)+(s.yours?' yours':s.name?' taken':'');b.disabled=!token||!controlReady()||Boolean(s.name&&!s.yours);
    const number=document.createElement('b');number.textContent=String(s.number).padStart(2,'0');
    const label=document.createElement('span'),name=document.createTextNode(s.name||'Open slot'),small=document.createElement('small');small.textContent=s.yours?'Your controller':s.name?'Player '+s.number:'Join as Player '+s.number;label.append(name,small);
    const mark=document.createElement('em');mark.textContent=s.yours?'YOU':s.name?'IN':'+';b.append(number,label,mark);
    b.addEventListener('click',async()=>{try{clearInput();await api('claim',{slot:n});await refresh();message('You are Player '+(n+1)+'. '+(n===0?'Use Enter / Start and Cross to navigate the game.':'Wait for Player 1 to choose multiplayer.'));}catch(e){message(e.message);}});return b;
  }));
  $('spectate').disabled=!token;$('spectate').textContent=mine<0?'Just watching':'Release my player slot';
  $('viewer-count').textContent=(roomState?.viewers||0)+' in room';
  if(controlReady())connected(mine>=0?'Player '+(mine+1)+' connected':'Watching',true);
}
// With no game running, players stay connected and the screen asks for a console and game.
// "Change game" opens the same list over a running game. Browsers only ever get titles, ids and covers.
let pickerOpen=false,library=null,libraryAt=0,picking=false,pickerDrawn='',bootChosen=false,consoleFilter='all',coverFor=null,fetchPoll=null;
const coverURLs=new Map(),coverQueue=[];let coverLoads=0;
async function loadLibrary(force=false){if(force)nowAt=0;if(force||!library||performance.now()-libraryAt>15000){libraryAt=performance.now();try{const fresh=await api('library');if(!bootChosen)$('boot-screen').checked=fresh.bootScreen;library=fresh;}catch(e){message(e.message);}}renderLibrary();}
// Covers need the room token, which an <img src> cannot send: fetch them and show blob URLs, a few at a time.
function coverKey(game){return game.id+':'+game.cover.version;}
function showCover(game){const url=coverURLs.get(coverKey(game));if(!url)return;for(const art of document.querySelectorAll(`#library [data-art="${game.id}"]`)){if(art.querySelector('img')?.src===url)continue;const img=new Image();img.alt='';img.src=url;art.replaceChildren(img);art.classList.remove('placeholder');}}
function queueCover(game){if(coverURLs.has(coverKey(game))){showCover(game);return;}if(!coverQueue.some(g=>coverKey(g)===coverKey(game)))coverQueue.push(game);void loadCovers();}
async function loadCovers(){
  while(coverLoads<4&&coverQueue.length&&token){const game=coverQueue.shift(),key=coverKey(game);coverLoads++;
    fetch('api/cover?id='+encodeURIComponent(game.id),{headers:{Authorization:'Bearer '+token}}).then(async r=>{if(!r.ok)return;
      const url=URL.createObjectURL(await r.blob());for(const [old,u] of coverURLs)if(old.startsWith(game.id+':')){URL.revokeObjectURL(u);coverURLs.delete(old);}coverURLs.set(key,url);showCover(game);
    }).catch(()=>{}).finally(()=>{coverLoads--;void loadCovers();});}
}
function forgetCovers(){coverQueue.length=0;coverURLs.forEach(u=>URL.revokeObjectURL(u));coverURLs.clear();}
function folded(text){return text.normalize('NFD').replace(/\p{M}/gu,'').toLocaleLowerCase();}
// Search and the console filter only hide cards, so typing never redraws the list or moves the focus.
function filterLibrary(){
  const query=folded($('game-search').value.trim());let shown=0;
  for(const box of $('library').children){
    const match=consoleFilter==='all'||box.dataset.console===consoleFilter;let inBox=0;
    for(const card of box.querySelectorAll('.game')){const on=match&&(!query||folded(card.dataset.title).includes(query));card.hidden=!on;if(on)inBox++;}
    box.hidden=!match||(Boolean(query)&&!inBox);shown+=inBox;
  }
  for(const chip of $('console-filter').children)chip.setAttribute('aria-pressed',String(chip.dataset.console===consoleFilter));
  const total=(library?.consoles||[]).reduce((n,c)=>n+c.games.length,0);
  mirrorSoon();
  $('no-match').hidden=!total||shown>0;$('no-match').textContent=query?'No games match “'+$('game-search').value.trim()+'”.':'No games for this console.';
}
function placeholder(art,game,item){art.classList.add('placeholder');const c=document.createElement('b'),t=document.createElement('span');c.textContent=item.id.toUpperCase();t.textContent=game.title;art.replaceChildren(c,t);}
// Details a metadata provider found: the year and genre on the card, the rest in the About panel.
function aboutLine(about,players=false){return [about?.year,about?.genre,players&&about?.players?about.players+' players':null].filter(Boolean).join(' · ');}
function showAbout(game){
  const panel=$('game-about'),title=document.createElement('h3'),line=document.createElement('p'),text=document.createElement('p'),close=document.createElement('button');
  title.textContent=game.title;line.className='about-line';line.textContent=aboutLine(game.about,true);text.textContent=game.about?.description||'';
  close.type='button';close.textContent='Close';close.addEventListener('click',()=>{panel.hidden=true;document.querySelector(`#library .game[data-id="${game.id}"] .pick`)?.focus();});
  panel.replaceChildren(title,...[line,text].filter(p=>p.textContent),close);panel.hidden=false;close.focus();
}
// Which provider entry a game is, for a provider that matches by name: the admin picks one when it is unsure, or fixes a wrong one.
function showMatch(game){
  const panel=$('game-about'),f=game.fetch||{},provider=library.matching,picked=f.picked?.[provider],title=document.createElement('h3'),now=document.createElement('p');
  title.textContent='Match for '+game.title;now.className='about-line';
  const label=m=>m.name+(m.year?' ('+m.year+')':'')+' · #'+m.id;
  if(picked===0)now.textContent='Set to no match: nothing is fetched from '+provider.toUpperCase()+'.';
  else if(f.match){now.textContent=(picked?'Chosen: ':'Matched: ')+label(f.match)+' ';
    if(f.match.url){const a=document.createElement('a');a.href=f.match.url;a.target='_blank';a.rel='noopener noreferrer';a.textContent='View';now.append(a);}}
  else now.textContent=f.candidates?.length?'Not sure which game this is. Choose one:':f.fetched?'No match found.':'Not looked up yet.';
  const picks=document.createElement('div');picks.className='match-picks';
  for(const c of f.candidates||[]){const b=document.createElement('button');b.type='button';b.textContent=label(c);b.addEventListener('click',()=>setMatch(game,c.id));picks.append(b);}
  const form=document.createElement('form'),field=document.createElement('label'),input=document.createElement('input'),use=document.createElement('button');
  form.className='match-form';field.textContent=provider.toUpperCase()+' game number';input.type='text';input.inputMode='numeric';input.pattern='[0-9]{1,9}';input.maxLength=9;input.required=true;input.autocomplete='off';
  field.append(input);use.type='submit';use.textContent='Use this game';form.append(field,use);
  form.addEventListener('submit',e=>{e.preventDefault();if(/^[1-9][0-9]{0,8}$/.test(input.value.trim()))void setMatch(game,Number(input.value.trim()));else message('Enter the number from the game’s '+provider.toUpperCase()+' page.');});
  const actions=document.createElement('div'),none=document.createElement('button'),auto=document.createElement('button'),close=document.createElement('button');actions.className='match-picks';
  none.type=auto.type=close.type='button';none.textContent='No match';auto.textContent='Match by name';close.textContent='Close';
  none.addEventListener('click',()=>setMatch(game,0));auto.addEventListener('click',()=>setMatch(game,null));auto.hidden=picked===undefined;
  close.addEventListener('click',()=>{panel.hidden=true;document.querySelector(`#library .game[data-id="${game.id}"] .pick`)?.focus();});
  actions.append(none,auto,close);panel.replaceChildren(title,now,...(picks.children.length?[picks]:[]),form,actions);panel.hidden=false;(picks.querySelector('button')||input).focus();
}
async function setMatch(game,entry){
  try{await api('game/match',{id:game.id,provider:library.matching,entry});$('game-about').hidden=true;
    message(entry===0?'No match for '+game.title+': its fetched details are cleared.':'Fetching '+game.title+' again with the new match.');}catch(e){message(e.message);}
  await loadLibrary(true);
}
async function fetchArt(body,what){
  try{const r=await api('cover/fetch',body);message(r.queued?'Fetching art for '+what+' in the background.':'Nothing to fetch: every game has a cover or was looked up already.');}catch(e){message(e.message);}
  await loadLibrary(true);
}
function renderLibrary(){
  const host=roomState?.host,list=$('library'),busy=picking||Boolean(host?.loading),can=Boolean(roomState?.canPick);
  const drawn=JSON.stringify([library,host?.game,host?.loading,host?.error,can,busy,roomState?.adminPin]);if(drawn===pickerDrawn)return;pickerDrawn=drawn;
  $('admin').hidden=can||!roomState?.adminPin;$('stop-game').hidden=!can||!host?.game;$('close-picker').hidden=!host?.game;
  $('picker-eyebrow').textContent=host?.loading?'STARTING':host?.game?'NOW PLAYING · '+host.game.title.toUpperCase():'NO GAME RUNNING';
  $('picker-note').textContent=host?.loading?'Starting '+host.loading+'… Everyone stays in the room.':host?.error&&!host.game?host.error:!library?.consoles.length?'The host has no game library. Start the host with --library to choose games here.':can?(host?.game?'Switching stops this game and saves its memory card first.':'Everyone stays in the room while a game starts.'):roomState?.adminPin?'The host chooses the game. Enter the host PIN to choose one yourself.':'The host has not started a game yet.';
  $('boot-option').hidden=!can||!library?.consoles.some(c=>c.bootScreen);
  const consoles=library?.consoles||[],focusedId=list.contains(document.activeElement)?document.activeElement.closest('.game')?.dataset.id:null;
  if(!consoles.some(c=>c.id===consoleFilter))consoleFilter='all';
  $('picker-tools').hidden=!consoles.some(c=>c.games.length);$('fetch-missing').hidden=!(can&&library?.metadata);
  $('console-filter').hidden=consoles.length<2;$('console-filter').replaceChildren(...(consoles.length<2?[]:[{id:'all',name:'All'},...consoles]).map(c=>{
    const b=document.createElement('button');b.type='button';b.dataset.console=c.id;b.textContent=c.id==='all'?'All':c.id.toUpperCase();b.title=c.name;
    b.addEventListener('click',()=>{consoleFilter=c.id;filterLibrary();});return b;}));
  list.replaceChildren();
  for(const item of consoles){
    const box=document.createElement('section'),title=document.createElement('h3'),games=document.createElement('div');box.className='console';box.dataset.console=item.id;games.className='games';title.textContent=item.name;box.append(title);
    const notes=[item.warning,item.available&&!item.bootScreen?'No boot screen for '+item.name+' on this host.':null].filter(Boolean);
    if(notes.length){const p=document.createElement('p');p.textContent=notes.join(' ');box.append(p);}
    if(item.available&&!item.games.length){const p=document.createElement('p');p.textContent='No games in this folder yet.';box.append(p);}
    for(const game of item.games){
      const card=document.createElement('div'),b=document.createElement('button'),art=document.createElement('span'),name=document.createElement('span'),playing=host?.game?.id===game.id;
      card.className='game'+(playing?' playing':'');card.dataset.id=game.id;card.dataset.title=game.title;
      b.type='button';b.className='pick';b.disabled=!can||busy;b.setAttribute('aria-label',(playing?'Playing: ':'Play ')+game.title);
      art.className='art';art.dataset.art=game.id;art.setAttribute('aria-hidden','true');name.className='name';name.textContent=game.title;
      placeholder(art,game,item);b.append(art,name);
      if(aboutLine(game.about)){const meta=document.createElement('span');meta.className='meta';meta.textContent=aboutLine(game.about);b.append(meta);}
      if(playing){const tag=document.createElement('em');tag.textContent='PLAYING';b.append(tag);}
      b.addEventListener('click',()=>pick(game,item));card.append(b);
      if(game.about?.description){const about=document.createElement('button');about.type='button';about.className='about';about.textContent='About';
        about.setAttribute('aria-label','About '+game.title);about.addEventListener('click',()=>showAbout(game));card.append(about);}
      // PS2: four players on the multitap, or two pads for a game that reads no multitap. The host keeps the choice.
      if(can&&typeof game.multitap==='boolean'){const tap=document.createElement('button');tap.type='button';tap.className='multitap';
        tap.textContent=game.multitap?'Multitap: 4 players':'Multitap off: 2 players';tap.setAttribute('aria-pressed',String(game.multitap));
        tap.setAttribute('aria-label','Multitap (4 players) for '+game.title);tap.disabled=busy;tap.addEventListener('click',()=>setMultitap(game,!game.multitap));card.append(tap);}
      if(can&&library.covers){
        const tools=document.createElement('div'),change=document.createElement('button');tools.className='cover-tools';
        change.type='button';change.textContent=game.cover?'Change cover':'Add cover';change.setAttribute('aria-label',change.textContent+' for '+game.title);
        change.addEventListener('click',()=>{coverFor=game;$('cover-file').value='';$('cover-file').click();});tools.append(change);
        if(game.cover?.uploaded){const remove=document.createElement('button');remove.type='button';remove.textContent='Remove';remove.setAttribute('aria-label','Remove the cover for '+game.title);
          remove.addEventListener('click',()=>removeCover(game));tools.append(remove);}
        if(library.metadata){const fetching=Boolean(game.fetch?.state),get=document.createElement('button');get.type='button';get.disabled=fetching;
          get.textContent=fetching?'Fetching…':game.fetch?.fetched?'Fetch again':'Fetch art';get.setAttribute('aria-label',get.textContent+' for '+game.title);
          get.addEventListener('click',()=>fetchArt({id:game.id},game.title));tools.append(get);}
        if(library.matching){const fix=document.createElement('button');fix.type='button';fix.disabled=Boolean(game.fetch?.state);fix.textContent='Match';
          fix.setAttribute('aria-label','Choose the match for '+game.title);fix.addEventListener('click',()=>showMatch(game));tools.append(fix);}
        card.append(tools);
        // A quiet note when a lookup found nothing or failed: the card keeps its placeholder.
        if(library.metadata&&game.fetch?.note&&!game.fetch.state){const note=document.createElement('p');note.className='fetch-note';note.textContent=game.fetch.note;card.append(note);}
      }
      games.append(card);
    }
    box.append(games);list.append(box);
  }
  filterLibrary();
  for(const item of consoles)for(const game of item.games)if(game.cover)queueCover(game);  // now the cards are in the page
  if(focusedId)list.querySelector(`.game[data-id="${focusedId}"] .pick:enabled`)?.focus();
  if(mirrorShown||mirrorIn?.s)drawMirror();
  // While art is being fetched, look again every few seconds.
  if(!fetchPoll&&consoles.some(c=>c.games.some(g=>g.fetch?.state)))fetchPoll=setTimeout(()=>{fetchPoll=null;if(token&&library&&!$('game-picker').hidden)void loadLibrary(true);},3000);
}
async function setMultitap(game,on){
  try{await api('game/multitap',{id:game.id,on});message(game.title+(on?' starts with the multitap: 4 players.':' starts with two pads, no multitap.'));}catch(e){message(e.message);}
  await loadLibrary(true);
}
async function uploadCover(game,file){
  if(file.size>5*1024*1024){message('Use an image of at most 5 MB.');return;}
  message('Uploading the cover for '+game.title+'…');
  try{const r=await fetch('api/cover?id='+encodeURIComponent(game.id),{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':file.type||'application/octet-stream'},body:file});
    if(!r.ok)throw new Error((await r.text()).slice(0,240));message('Cover saved for '+game.title+'.');}catch(e){message(e.message);}
  await loadLibrary(true);
}
async function removeCover(game){
  if(!confirm('Remove the cover for '+game.title+'?'))return;
  try{await api('cover/remove',{id:game.id});message('Cover removed for '+game.title+'.');}catch(e){message(e.message);}
  await loadLibrary(true);
}
$('cover-file').addEventListener('change',()=>{const file=$('cover-file').files[0];if(file&&coverFor)void uploadCover(coverFor,file);coverFor=null;});
$('game-search').addEventListener('input',filterLibrary);
$('fetch-missing').addEventListener('click',()=>fetchArt({missing:true},'every game with no cover'));
// Arrow keys and a gamepad's D-pad move between cards by position; Enter, Space or the pad's ✕ button picks.
function pickerTargets(){return [...$('game-picker').querySelectorAll('button:enabled,input:not([type=checkbox]):not([hidden])')].filter(e=>e.offsetParent&&!e.closest('[hidden]'));}
function moveFocus(dx,dy){
  const items=pickerTargets(),from=document.activeElement;if(!items.length)return;
  if(!items.includes(from)){(items.find(e=>e.classList.contains('pick'))||items[0]).focus();return;}
  const a=from.getBoundingClientRect(),ax=a.left+a.width/2,ay=a.top+a.height/2;let best=null,score=Infinity;
  for(const e of items){if(e===from)continue;const b=e.getBoundingClientRect(),x=b.left+b.width/2-ax,y=b.top+b.height/2-ay,along=dx?x*dx:y*dy,across=dx?Math.abs(y):Math.abs(x);
    if(along<=1)continue;const s=along+across*3;if(s<score){score=s;best=e;}}
  if(best){best.focus();best.scrollIntoView({block:'nearest'});}
}
const arrows={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]};
$('game-picker').addEventListener('keydown',event=>{
  const search=event.target===$('game-search');
  if(search&&event.key==='Escape'&&event.target.value){event.target.value='';filterLibrary();event.preventDefault();return;}
  if(!(event.key in arrows)||(search&&event.key!=='ArrowDown')||event.target.tagName==='INPUT'&&!search)return;
  event.preventDefault();moveFocus(...arrows[event.key]);
});
const padNav={held:'',at:0};
function pollPicker(){
  if($('game-picker').hidden||!focused){padNav.held='';return;}
  const pad=selectedPad();if(!pad||pad.mapping!=='standard')return;
  const b=n=>pad.buttons[n]?.pressed,x=pad.axes[0]||0,y=pad.axes[1]||0;
  const dir=b(12)||y<-.5?'up':b(13)||y>.5?'down':b(14)||x<-.5?'left':b(15)||x>.5?'right':'';
  const held=dir||(b(0)?'pick':b(1)?'back':''),now=performance.now();
  if(held!==padNav.held){padNav.held=held;padNav.at=now+350;}else if(!dir||now<padNav.at)return;else padNav.at=now+140;
  if(dir)moveFocus(...{up:[0,-1],down:[0,1],left:[-1,0],right:[1,0]}[dir]);
  else if(held==='pick'&&$('game-picker').contains(document.activeElement)&&document.activeElement.tagName==='BUTTON')document.activeElement.click();
  else if(held==='back'&&!$('close-picker').hidden)$('close-picker').click();
}
async function pick(game,item){
  const host=roomState?.host;if(host?.game&&!confirm('Switch to '+game.title+'? '+host.game.title+' stops for everyone and its memory card is saved.'))return;
  picking=true;renderLibrary();clearInput();
  try{await api('game',{id:game.id,bootScreen:item.bootScreen&&$('boot-screen').checked});pickerOpen=false;pickerTouched=0;message('Now playing '+game.title+'.');}catch(e){message(e.message);}finally{picking=false;await refresh();}
}
function renderGame(){
  const game=roomState?.host.game,show=Boolean(token&&roomState&&(!game||pickerOpen));
  $('change-game').hidden=!token||!game||!roomState?.canPick&&!roomState?.adminPin;$('game-picker').hidden=!show;$('screen').classList.toggle('picking',show);fitSideways();
  if(show)void loadLibrary();
}
// Room activity: who has the picker open, and the game starting or just stopped, for everyone in the room.
// The chooser's page reports its picker; the host checks the right, rate-limits it and forgets a silent one.
let pickerSent={open:false,at:0},pickerTouched=0,activityArt={key:'',url:null};
// With no game running the picker shows for everyone, so only a player using it counts as choosing:
// one who opened it over a running game, or who has touched it in the last 20 seconds.
for(const type of ['pointerdown','keydown','input','focusin','mouseover'])$('game-picker').addEventListener(type,()=>{if(!mirrorShown)pickerTouched=performance.now();},{passive:true});
function choosingOpen(){
  const using=pickerOpen||performance.now()-pickerTouched<20000;
  return Boolean(token&&roomState?.canPick&&!roomState.host.loading&&!picking&&!mirrorShown&&using&&!$('game-picker').hidden&&document.visibilityState==='visible');
}
function reportPicker(){
  const open=choosingOpen(),at=performance.now();mirrorTick(open);
  if(!open&&!pickerSent.open)return;
  if(open===pickerSent.open&&(!open||at-pickerSent.at<5000))return;
  pickerSent={open,at};api('picker',{open}).catch(()=>{});
}
document.addEventListener('visibilitychange',reportPicker);
// Picker mirror. The chooser's page sends what its picker shows, at most 20 times a second and only when that
// changes: the search box's text (never keys), the console filter, the scroll, the game under the pointer or
// focus, and the pointer. Nothing else is read, so the PIN and every other field never leave the page.
const pickerCard=document.querySelector('.picker-card'),round3=v=>Math.round(Math.min(Math.max(v,0),1)*1000)/1000;
const mirrorOut={on:false,last:'',at:0,timer:null,p:null,o:null,over:null,pointerAt:0};
function layoutKey(){const grid=[...$('library').querySelectorAll('.games')].find(g=>g.offsetParent);return (phone.matches?'p':'d')+(grid?Math.min(getComputedStyle(grid).gridTemplateColumns.split(' ').length,99):0);}
function mirrorSnapshot(){
  const room=pickerCard.scrollHeight-pickerCard.clientHeight,live=performance.now()-mirrorOut.pointerAt<3000;  // a still pointer goes after 3 s
  const focusedCard=$('library').contains(document.activeElement)?document.activeElement.closest('.game')?.dataset.id:null;
  return {q:$('game-search').value.slice(0,40),c:consoleFilter,y:room>0?round3(pickerCard.scrollTop/room):0,
    h:(live&&mirrorOut.over)||focusedCard||null,p:live?mirrorOut.p:null,o:live&&mirrorOut.over?mirrorOut.o:null,l:layoutKey()};
}
function mirrorFlush(force=false){
  mirrorOut.timer=null;if(!mirrorOut.on)return;
  const s=mirrorSnapshot(),text=JSON.stringify(s);
  if(text===mirrorOut.last&&!force)return;
  if(send({t:'mirror',s})){mirrorOut.last=text;mirrorOut.at=performance.now();}
}
function mirrorSoon(){if(mirrorOut.on&&!mirrorOut.timer)mirrorOut.timer=setTimeout(mirrorFlush,Math.max(0,50-(performance.now()-mirrorOut.at)));}
function mirrorTick(open){
  if(open&&!mirrorOut.on){mirrorOut.on=true;mirrorOut.last='';mirrorSoon();}
  else if(!open&&mirrorOut.on){mirrorOut.on=false;clearTimeout(mirrorOut.timer);mirrorOut.timer=null;
    if(!picking&&!roomState?.host.loading)send({t:'mirror',s:null});}  // after a pick the host shows the choice, then ends it
  else if(open){if(performance.now()-mirrorOut.at>2000)mirrorFlush(true);else mirrorSoon();}
}
pickerCard.addEventListener('pointermove',e=>{
  if(e.pointerType==='touch'||!mirrorOut.on)return;  // no pointer to show from a touch screen
  const c=pickerCard.getBoundingClientRect(),card=e.target.closest?.('#library .game');
  mirrorOut.p=[round3((e.clientX-c.left)/c.width),round3((e.clientY-c.top)/c.height)];
  if(card){const r=card.getBoundingClientRect();mirrorOut.over=card.dataset.id;mirrorOut.o=[round3((e.clientX-r.left)/r.width),round3((e.clientY-r.top)/r.height)];}
  else mirrorOut.over=mirrorOut.o=null;
  mirrorOut.pointerAt=performance.now();mirrorSoon();
},{passive:true});
pickerCard.addEventListener('pointerleave',()=>{mirrorOut.p=mirrorOut.o=mirrorOut.over=null;mirrorSoon();});
pickerCard.addEventListener('scroll',mirrorSoon,{passive:true});
$('library').addEventListener('focusin',mirrorSoon);$('library').addEventListener('focusout',mirrorSoon);
$('game-search').addEventListener('input',mirrorSoon);
// Watching: the chooser's list opens by itself for everyone else, and closes when they close it, pick or drop.
// - full: read-only in this page's own games list, with their pointer. Only while no game runs, and not
//   while this player is typing or using their own list: that is never taken from them.
// - compact: a small panel in the activity strip, never over the stream or the touch controls, and no input
//   is held back from the game.
// - min: one line, once this player has hidden it; the next choosing session opens again.
// A different layout (a phone watching a desktop) follows the chooser by game card rather than by position.
let mirrorIn=null,mirrorShown=false,mirrorOwn=null,mirrorHidden=null,mirrorTitles={at:-1e9,games:{}},pickerUsed=-1e9;
// Using one's own list is a click, a key, typing or scrolling it, not a pointer resting over it.
for(const type of ['pointerdown','keydown','input','wheel'])$('game-picker').addEventListener(type,()=>{if(!mirrorShown)pickerUsed=performance.now();},{passive:true});
function mirrorReceive(data){
  if(!data||!Number.isInteger(data.n)||mirrorIn&&data.n<=mirrorIn.n)return;
  mirrorIn={n:data.n,id:data.id??null,name:data.name,s:data.s||null};
  drawMirror();renderActivity();
}
function typingHere(){
  const el=document.activeElement;if(!el||mirrorShown&&$('picker-tools').contains(el))return false;
  return el.isContentEditable||el.tagName==='TEXTAREA'||el.tagName==='SELECT'||el.tagName==='INPUT'&&!/^(button|checkbox|radio|submit|reset|range|file)$/.test(el.type);
}
function mirrorMode(){
  const s=mirrorIn?.s;if(!s||!token||!roomState)return '';
  if(s.sel)return mirrorShown||'';  // "Starting" says it now
  if(mirrorHidden===mirrorIn.id)return 'min';
  // Over a running game the list is docked beside the stream, read-only, so the game keeps its input.
  if((roomState.host.game||roomState.host.loading)&&$('game-picker').hidden)return 'dock';
  const busy=typingHere()||!mirrorShown&&($('game-picker').contains(document.activeElement)||performance.now()-pickerUsed<5000);
  return !roomState.host.game&&!roomState.host.loading&&!$('game-picker').hidden&&!busy?'full':'compact';
}
function hideMirror(){mirrorHidden=mirrorIn?.id??null;drawMirror();renderActivity();}
function showMirror(){mirrorHidden=null;drawMirror();renderActivity();}
document.addEventListener('focusin',()=>{if(mirrorIn?.s){drawMirror();renderActivity();}});
// Titles for the compact panel, when this page has not loaded the list itself.
function mirrorTitle(id){
  if(!id)return null;
  for(const c of library?.consoles||[])for(const g of c.games)if(g.id===id)return {title:g.title,console:c.name};
  if(!mirrorTitles.games[id]&&performance.now()-mirrorTitles.at>30000){mirrorTitles.at=performance.now();
    api('library').then(body=>{const games={};for(const c of body.consoles||[])for(const g of c.games)games[g.id]={title:g.title,console:c.name};
      mirrorTitles.games=games;renderActivity();}).catch(()=>{});}
  return mirrorTitles.games[id]||null;
}
function revealInPicker(el){const c=pickerCard.getBoundingClientRect(),r=el.getBoundingClientRect();
  if(r.top<c.top+8)pickerCard.scrollTop-=c.top+8-r.top;else if(r.bottom>c.bottom-8)pickerCard.scrollTop+=r.bottom-c.bottom+8;}
function placeGhost(s,target,same){
  const ghost=$('ghost'),base=ghost.parentElement.getBoundingClientRect();let x,y;
  if(target&&(s.o||s.sel)){const r=target.getBoundingClientRect(),o=s.o||[.5,.5];x=r.left+o[0]*r.width;y=r.top+o[1]*r.height;}
  else if(same&&s.p){const c=pickerCard.getBoundingClientRect();x=c.left+s.p[0]*c.width;y=c.top+s.p[1]*c.height;}
  else{ghost.hidden=true;return;}
  ghost.lastElementChild.textContent=mirrorIn.name;ghost.hidden=false;ghost.style.transform=`translate(${Math.round(x-base.left)}px,${Math.round(y-base.top)}px)`;
}
function drawMirror(){
  const mode=mirrorMode();if(mirrorShown&&mirrorShown!==mode)unmirror();
  if(mode!=='full'&&mode!=='dock')return;
  const s=mirrorIn.s;
  if(!mirrorShown){mirrorShown=mode;mirrorOwn={q:$('game-search').value,c:consoleFilter,y:pickerCard.scrollTop};pickerCard.classList.add('mirrored');
    if(mode==='dock')dockPicker(true);else mirrorLock(true);}
  if(mode==='dock'&&!library)void loadLibrary();  // it draws the mirror again once the list is in
  $('game-search').value=s.q;consoleFilter=library?.consoles.some(c=>c.id===s.c)?s.c:'all';filterLibrary();
  let target=null;
  for(const card of $('library').querySelectorAll('.game')){const id=card.dataset.id;
    card.classList.toggle('watched',id===s.h&&!s.sel);card.classList.toggle('chosen',id===s.sel);if(id===(s.sel||s.h)&&!card.hidden)target=card;}
  const same=s.l===layoutKey();
  if(same||!target){const room=pickerCard.scrollHeight-pickerCard.clientHeight;pickerCard.scrollTop=Math.round(s.y*room);}
  if(target&&(!same||s.sel))revealInPicker(target);
  placeGhost(s,target,same);
}
// Read-only: the list and the search bar's controls, not the label pinned in the bar.
function mirrorLock(on){for(const el of [$('library'),...$('picker-tools').children])if(el!==$('activity'))el.inert=on;}
// Over a running game the same list moves out of the screen into a dock under the room strip: inert, so it
// takes no focus, clicks or keys, and #game-picker stays hidden, so the game's keyboard and gamepad still work.
const pickerDock=document.createElement('div');pickerDock.className='picker-dock';
function dockPicker(on){
  if(on){pickerDock.append(pickerCard,$('ghost'));pickerCard.inert=true;placeActivity();}
  else{$('game-picker').append(pickerCard,$('ghost'));pickerCard.inert=false;pickerDock.remove();}
}
function unmirror(){
  if(mirrorShown==='dock')dockPicker(false);else mirrorLock(false);
  mirrorShown=false;pickerCard.classList.remove('mirrored');$('ghost').hidden=true;
  for(const card of $('library').querySelectorAll('.watched,.chosen'))card.classList.remove('watched','chosen');
  if(mirrorOwn){$('game-search').value=mirrorOwn.q;consoleFilter=mirrorOwn.c;filterLibrary();pickerCard.scrollTop=mirrorOwn.y;mirrorOwn=null;}
}
function activityArtwork(span,game){
  const key=game.cover?game.id+':'+game.cover:'';
  const mark=()=>{const b=document.createElement('b');b.textContent=(game.console||'').toUpperCase();span.className='activity-art placeholder';span.replaceChildren(b);};
  if(key&&key===activityArt.key&&activityArt.url){const img=new Image();img.alt='';img.src=activityArt.url;span.className='activity-art';span.replaceChildren(img);return;}
  mark();if(!key)return;
  fetch('api/cover?id='+encodeURIComponent(game.id),{headers:{Authorization:'Bearer '+token}}).then(async r=>{if(!r.ok)return;
    const url=URL.createObjectURL(await r.blob());if(activityArt.url)URL.revokeObjectURL(activityArt.url);activityArt={key,url};
    if(span.isConnected){const img=new Image();img.alt='';img.src=url;span.className='activity-art';span.replaceChildren(img);}}).catch(()=>{});
}
let activityDrawn='';
function placeActivity(){
  // Always in view and never over the stream: at the top of the games list while that shows, else at the top
  // of the side column on a wide screen, else under the stream.
  const box=$('activity'),aside=$('now-playing').parentElement;
  // While watching it stays pinned in the search bar, so the chooser's name scrolls with nothing.
  if(mirrorShown==='full'){if($('picker-tools').firstElementChild!==box)$('picker-tools').prepend(box);}
  else if(!$('game-picker').hidden){if(box.previousElementSibling!==$('picker-note'))$('picker-note').after(box);}
  else if(!phone.matches){if(aside.firstElementChild!==box)aside.prepend(box);}
  else{const pad=$('touch-pad'),home=pad.parentElement!==$('screen')&&getComputedStyle(pad).display!=='none'?pad:document.querySelector('.screen-bar');
    if(box.previousElementSibling!==home)home.after(box);}  // a phone: under the stream, and under its touch controls
  if(mirrorShown==='dock'&&box.nextElementSibling!==pickerDock)box.after(pickerDock);
}
function renderActivity(){
  placeActivity();const box=$('activity');
  const change=token?roomState?.change:null,others=token?(roomState?.choosing||[]).filter(c=>!c.yours):[],mode=mirrorMode();
  const live=mode&&!mirrorIn.s.sel?mirrorIn.name:null,names=[...new Set([...(live?[live]:[]),...others.map(c=>c.name)])];
  const s=mode==='compact'?mirrorIn.s:null,looking=s?mirrorTitle(s.h):null;
  const drawn=JSON.stringify([change,names,Boolean(roomState?.host.loading),mode,s&&[s.q,s.c,looking]]);if(drawn===activityDrawn)return;activityDrawn=drawn;
  const rows=[];
  if(change){
    const row=document.createElement('div'),art=document.createElement('span'),text=document.createElement('span'),eyebrow=document.createElement('small'),title=document.createElement('strong'),by=document.createElement('span');
    row.className='activity-change '+change.kind;text.className='activity-text';
    eyebrow.textContent=change.kind==='starting'?'STARTING':'STOPPED';title.textContent=change.game.title;
    by.textContent=change.kind==='starting'?(roomState.host.loading?'Loading the game':'The console is starting up')+' · chosen by '+change.by:'Stopped by '+change.by+'. Its memory card is saved.';
    text.append(eyebrow,title,by);row.append(art,text);rows.push(row);
    if(change.kind==='starting')activityArtwork(art,change.game);else art.remove();
  }
  if(names.length){
    const row=document.createElement('div'),dot=document.createElement('i'),text=document.createElement('span');row.className='activity-choosing'+(s?' compact':'');
    const list=names.length>2?names.slice(0,-1).join(', ')+' and '+names.at(-1):names.join(' and ');
    const head=document.createElement('span');head.textContent=list+(names.length>1?' are':' is')+' choosing a game…'+(mode==='full'||mode==='dock'?' · watching live':'');text.append(head);
    if(s){  // what the chooser's list shows, in a line or two
      const what=[s.q?'Searching “'+s.q+'”':'',s.c!=='all'?'Showing '+s.c.toUpperCase():''].filter(Boolean).join(' · ');
      if(what){const line=document.createElement('small');line.textContent=what;text.append(line);}
      if(looking){const line=document.createElement('small'),b=document.createElement('b');b.textContent=looking.title;line.append('Looking at ',b,' · '+looking.console);text.append(line);}
    }
    text.className='activity-text';row.append(dot,text);
    if(live){const b=document.createElement('button');b.type='button';b.textContent=mode==='min'?'Show':'Hide';
      b.setAttribute('aria-label',(mode==='min'?'Show':'Hide')+' what '+live+' is choosing');
      b.addEventListener('click',mode==='min'?showMirror:hideMirror);row.append(b);}
    rows.push(row);
  }
  box.replaceChildren(...rows);box.hidden=!rows.length;
}
// Now playing: the running game's cover, title, console, time and players, plus any details it has.
// Everything is drawn with textContent; the host checks the admin right and the lengths again.
const phone=matchMedia('(max-width:700px)');
let now=null,nowAt=0,nowLoading=false,nowStarted=0,nowShown=null,nowArt={key:'',url:null};
const NOW_FACTS=[['year','Released'],['genre','Genre'],['players','Players'],['publisher','Publisher']];
function runningFor(ms){const m=Math.floor(ms/60000);return m<1?'just started':m<60?m+' min':Math.floor(m/60)+' h '+String(m%60).padStart(2,'0')+' min';}
function nowArtwork(game){
  const art=$('np-art'),key=game.cover?game.id+':'+game.cover.version:'none:'+game.id;
  if(key===nowArt.key)return;nowArt.key=key;if(nowArt.url){URL.revokeObjectURL(nowArt.url);nowArt.url=null;}
  const fallback=()=>{const c=document.createElement('b');c.textContent=game.consoleId.toUpperCase();art.className='np-art placeholder';art.dataset.console=game.consoleId;art.replaceChildren(c);};
  fallback();if(!game.cover)return;
  fetch('api/cover?id='+encodeURIComponent(game.id),{headers:{Authorization:'Bearer '+token}}).then(async r=>{if(!r.ok||nowArt.key!==key)return;
    const url=URL.createObjectURL(await r.blob());if(nowArt.key!==key){URL.revokeObjectURL(url);return;}nowArt.url=url;
    const back=new Image(),front=new Image();back.alt=front.alt='';back.className='np-back';back.src=front.src=url;art.className='np-art';art.replaceChildren(back,front);
  }).catch(()=>{});
}
function drawNowPlaying(){
  const game=now?.game;if(!game)return;
  $('np-title').textContent=game.title;$('np-short').textContent=game.title;$('np-console').textContent=game.console;
  const facts=NOW_FACTS.filter(([key])=>game.about?.[key]).map(([key,label])=>{const row=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=game.about[key];row.append(dt,dd);return row;});
  $('np-facts').replaceChildren(...facts);$('np-facts').hidden=!facts.length;
  $('np-description').textContent=game.about?.description||'';$('np-description').hidden=!game.about?.description;
  $('np-edit').hidden=!now.canEdit||!$('np-form').hidden;nowArtwork(game);
}
async function loadNowPlaying(){
  if(nowLoading||!token)return;nowLoading=true;nowAt=performance.now();
  try{const fresh=await api('playing');if(fresh.game&&fresh.game.id===roomState?.host.game?.id){now=fresh;drawNowPlaying();}}catch{}finally{nowLoading=false;}
}
function renderNowPlaying(){
  const running=token?roomState?.host.game:null,panel=$('now-playing');
  if(!running?.id){if(nowShown){panel.hidden=true;$('np-form').hidden=true;now=null;nowShown=null;nowArt.key='';if(nowArt.url)URL.revokeObjectURL(nowArt.url);nowArt.url=null;}return;}
  const started=performance.now()-running.runningS*1000;
  if(running.id!==nowShown){nowShown=running.id;now=null;nowStarted=started;$('np-form').hidden=true;
    // A new game: the title at once, the rest when it loads. Folded on a phone, so the touch controls stay in view.
    $('np-title').textContent=$('np-short').textContent=running.title;$('np-facts').replaceChildren();$('np-description').textContent='';$('np-edit').hidden=true;
    if(panel.hidden&&phone.matches)panel.open=false;panel.hidden=false;void loadNowPlaying();}
  else if(Math.abs(started-nowStarted)>3000)nowStarted=started;
  else if(performance.now()-nowAt>20000)void loadNowPlaying();
  $('np-running').textContent='running '+runningFor(performance.now()-nowStarted);
  const players=(roomState.slots||[]).filter(s=>s.name).map(s=>{const li=document.createElement('li'),b=document.createElement('b'),name=document.createElement('span');li.className='p'+((s.number-1)%4+1);b.textContent='P'+s.number;name.textContent=s.name+(s.yours?' (you)':'');li.append(b,name);return li;});
  if(!players.length){const li=document.createElement('li');li.className='np-none';li.textContent='No one has a controller yet';players.push(li);}
  $('np-players').replaceChildren(...players);
}
$('np-edit').addEventListener('click',()=>{const about=now?.game?.about||{};$('np-edit-description').value=about.description||'';$('np-edit-year').value=about.year||'';$('np-edit-genre').value=about.genre||'';$('np-form').hidden=false;$('np-edit').hidden=true;$('now-playing').open=true;$('np-edit-description').focus();});
$('np-cancel').addEventListener('click',()=>{$('np-form').hidden=true;drawNowPlaying();$('np-edit').focus();});
$('np-form').addEventListener('submit',async event=>{
  event.preventDefault();const game=now?.game;if(!game)return;
  // Only what changed: an untouched fetched value stays the provider's, and an emptied field goes back to it.
  const changes={};for(const key of ['description','year','genre']){const value=$('np-edit-'+key).value.trim();if(value!==(game.about?.[key]||'')||value===''&&game.edited.includes(key))changes[key]=value;}
  if(!Object.keys(changes).length){$('np-form').hidden=true;drawNowPlaying();return;}
  try{const r=await api('game/about',{id:game.id,...changes});game.about=r.about;game.edited=r.edited;$('np-form').hidden=true;drawNowPlaying();message('Details saved for '+game.title+'.');libraryAt=0;}catch(e){message(e.message);}
});
async function refresh(){
  if(!token||polling)return;polling=true;
  try{roomState=await api('state');renderSlots();renderGame();mirrorReceive(roomState.mirror);drawMirror();renderNowPlaying();renderActivity();reportPicker();const hz=roomState.host.emulationHz;$('host-info').textContent=!roomState.host.game?'No game loaded':roomState.host.core+' · '+(Number.isFinite(hz)?Math.round(hz)+'/':'')+roomState.host.fps+' Hz game'+(roomState.host.multitap==='none'?'':' · '+roomState.host.multitap+' multitap');if(roomState.host.console){$('pad-label').textContent='KEYBOARD → '+roomState.host.pad.toUpperCase();setAnalog(!!roomState.host.analog);$('game-label').textContent='ORIGINAL '+roomState.host.console+' GAME · BROWSER STREAMING';}if(roomState.host.error&&roomState.host.game)message('The emulator stopped: '+roomState.host.error);renderDiagnostics();}
  catch(e){message(e.message);}finally{polling=false;}
}
async function leaveRoom(notify=true){
  clearInput();const old=token;token=null;mine=-1;roomState=null;forgetCovers();library=null;pickerDrawn='';
  pickerSent={open:false,at:0};pickerTouched=0;mirrorOut.on=false;mirrorIn=null;mirrorHidden=null;unmirror();renderActivity();
  if(old)try{await fetch('api/leave',{method:'POST',headers:{Authorization:'Bearer '+old,'Content-Type':'application/json'},body:'{}'});}catch{}
  const oldPC=pc;pc=null;channel=null;if(oldPC)oldPC.close();
  const oldSocket=socket;socket=null;fallbackReady=false;if(oldSocket)oldSocket.close();
  latestJPEG=null;$('fallback').hidden=true;video.hidden=false;
  if(audioContext){void audioContext.close().catch(()=>{});audioContext=null;audioGain=null;}muted=true;audioAt=0;video.muted=true;$('sound').textContent='Enable sound';
  incoming.getTracks().forEach(t=>{t.stop();incoming.removeTrack(t);});video.srcObject=null;
  incomingAudio.getTracks().forEach(t=>{t.stop();incomingAudio.removeTrack(t);});rtcAudio.srcObject=null;rtcAudio.muted=true;
  lastSentMask=null;lastSentAt=0;receiverBufferMs=null;receivedToDisplayMs=null;previousJitter=null;
  pickerOpen=false;library=null;bootChosen=false;$('change-game').hidden=true;renderNowPlaying();
  $('join-panel').hidden=false;$('game-picker').hidden=true;$('loading').hidden=true;$('join').disabled=false;$('sound').disabled=true;$('leave').disabled=true;
  $('screen').classList.remove('playing');fitSideways();
  $('stream-info').textContent='Waiting for a stream';document.querySelector('.live').classList.remove('active');connected('Not connected');renderSlots();
  if(notify)message('You left the room. Your player slot has been released.');
}
function renderDiagnostics(){
  const ms=value=>Number.isFinite(value)?Math.round(value)+' ms':'—';
  const h=roomState?.host;
  const rows=[['Stream',controlReady()?(transport==='webrtc'?'WebRTC':'WebSocket fallback'):'Disconnected'],
    ['Input connection RTT',ms(rtt)],['Input wait for emulator',ms(roomState?.inputPollMs)],
    ['Video receiver buffer',ms(receiverBufferMs)],['Video receive to display',ms(receivedToDisplayMs)],
    ['Host source frame age',ms(roomState?.sourceFrameAgeMs)],['Host audio queue',ms(h?.audioQueueMs)],
    ['Emulator rate',Number.isFinite(h?.emulationHz)?h.emulationHz.toFixed(1)+' / '+h.fps+' Hz':'—'],
    ['Work per game tick',ms(h?.emulationWorkMs)]];
  $('diagnostics').replaceChildren(...rows.map(([name,value])=>{const row=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=name;dd.textContent=value;row.append(dt,dd);return row;}));
}
function preferFreshPlayback(receiver){
  // Optional browser hints; unsupported browsers keep their default behavior.
  for(const name of ['jitterBufferTarget','playoutDelayHint'])if(name in receiver)try{receiver[name]=0;}catch{}
}
if(video.requestVideoFrameCallback){
  const displayed=(now,metadata)=>{const delay=metadata.expectedDisplayTime-metadata.receiveTime;if(Number.isFinite(delay)&&delay>=0&&delay<30000)receivedToDisplayMs=delay;video.requestVideoFrameCallback(displayed);};
  video.requestVideoFrameCallback(displayed);
}
async function drawJPEG(){
  if(decoding)return;decoding=true;
  try{while(latestJPEG){const data=latestJPEG;latestJPEG=null;const image=await createImageBitmap(new Blob([data],{type:'image/jpeg'}));if(transport==='websocket'&&fallbackReady){$('fallback').getContext('2d').drawImage(image,0,0,640,480);frames++;}image.close();}}
  catch{message('Could not decode a video frame.');}finally{decoding=false;}
}
function playPCM(data){
  audioPackets++;
  if(muted||!audioContext||audioContext.state!=='running')return;
  const samples=new Int16Array(data),count=samples.length/2,now=audioContext.currentTime;
  if(audioAt>now+.15)return; // Drop excess audio rather than growing latency.
  const buffer=audioContext.createBuffer(2,count,sampleRate);
  for(let c=0;c<2;c++){const out=buffer.getChannelData(c);for(let n=0;n<count;n++)out[n]=samples[n*2+c]/32768;}
  const source=audioContext.createBufferSource();source.buffer=buffer;source.connect(audioGain);source.onended=()=>source.disconnect();
  audioAt=Math.max(audioAt,now+.025);source.start(audioAt);audioAt+=count/sampleRate;
}
async function connectSocket(){
  if(pc){pc.close();pc=null;channel=null;}transport='websocket';
  video.hidden=true;$('fallback').hidden=false;
  const scheme=location.protocol==='https:'?'wss:':'ws:';socket=new WebSocket(scheme+'//'+location.host+new URL('api/socket',location.href).pathname);socket.binaryType='arraybuffer';const ws=socket;
  await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('The fallback stream could not connect.')),12000);
    ws.addEventListener('open',()=>ws.send(JSON.stringify({token})));
    ws.addEventListener('message',event=>{
      if(typeof event.data==='string'){
        try{const data=JSON.parse(event.data);if(data.t==='ready'){sampleRate=data.sampleRate;fallbackReady=true;clearTimeout(timer);resolve();}else if(data.t==='pong')rtt=Math.round(performance.now()-data.sent);else if(data.t==='mirror')mirrorReceive(data);}catch{}
      }else{const bytes=new Uint8Array(event.data);if(bytes[0]===1){latestJPEG=event.data.slice(1);void drawJPEG();}else if(bytes[0]===2)playPCM(event.data.slice(1));}
    });
    ws.addEventListener('error',()=>{clearTimeout(timer);reject(new Error('The host connection failed.'));});
    ws.addEventListener('close',()=>{clearTimeout(timer);if(socket!==ws)return;fallbackReady=false;clearInput();connected('Connection interrupted');message('The host disconnected. Leave and rejoin to try again.');reject(new Error('The host disconnected.'));});
  });
}
// A relay that the network blocks keeps gathering open. After the timeout, use the candidates found so far.
async function waitForICE(peer){
  if(peer.iceGatheringState==='complete')return;
  await new Promise(resolve=>{const timer=setTimeout(()=>{peer.removeEventListener('icegatheringstatechange',change);resolve();},15000);
    function change(){if(peer.iceGatheringState==='complete'){clearTimeout(timer);peer.removeEventListener('icegatheringstatechange',change);resolve();}}peer.addEventListener('icegatheringstatechange',change);change();});
}
$('join-form').addEventListener('submit',async event=>{
  event.preventDefault();if(joining)return;joining=true;$('join').disabled=true;$('loading').hidden=false;connected('Connecting');
  try{
    const joined=await api('join',{name:$('name').value.trim(),pin:$('pin').value});token=joined.token;
    pc=new RTCPeerConnection({iceServers:joined.iceServers});const peer=pc;
    peer.addTransceiver('video',{direction:'recvonly'});peer.addTransceiver('audio',{direction:'recvonly'});
    channel=peer.createDataChannel('controls',{ordered:false,maxRetransmits:0});
    channel.addEventListener('message',event=>{try{const data=JSON.parse(event.data);if(data.t==='pong')rtt=Math.round(performance.now()-data.sent);else if(data.t==='mirror')mirrorReceive(data);}catch{}});
    peer.addEventListener('track',event=>{
      preferFreshPlayback(event.receiver);
      if(event.track.kind==='video'){incoming.addTrack(event.track);video.srcObject=incoming;video.play().catch(()=>message('Tap the video to start playback.'));}
      else{incomingAudio.addTrack(event.track);rtcAudio.srcObject=incomingAudio;rtcAudio.muted=muted;rtcAudio.play().catch(()=>{});}
    });
    peer.addEventListener('connectionstatechange',()=>{if(peer!==pc)return;if(['failed','disconnected'].includes(peer.connectionState)){clearInput();connected('Connection interrupted');message('Stream interrupted. Leave and rejoin; check the host firewall or ICE configuration.');}});
    await peer.setLocalDescription(await peer.createOffer());
    transport='webrtc';frames=0;lastFrames=0;audioPackets=0;lastStats=performance.now();
    try{
      await waitForICE(peer);
      if(!peer.localDescription.sdp.includes('a=candidate:'))throw new Error('No WebRTC network candidates');
      const answer=await api('offer',{sdp:peer.localDescription.sdp,type:'offer'});await peer.setRemoteDescription(answer);
      await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('WebRTC did not connect')),8000);if(channel.readyState==='open'){clearTimeout(timer);resolve();}else channel.addEventListener('open',()=>{clearTimeout(timer);resolve();},{once:true});});
    }catch{message('Using the compatible stream connection…');await connectSocket();}
    await refresh();const free=roomState.slots.findIndex(s=>!s.name);if(free>=0){await api('claim',{slot:free});await refresh();}
    $('join-panel').hidden=true;$('loading').hidden=true;$('sound').disabled=false;$('leave').disabled=false;connected(mine>=0?'Player '+(mine+1)+' connected':'Watching',true);
    $('screen').classList.add('playing');fitSideways();
    message(mine===0?'You are Player 1. Use Enter / Start and Cross to navigate the original game.':mine>0?'You are Player '+(mine+1)+'. Wait for Player 1 to choose multiplayer.':'All player slots are occupied. You can watch the game.');
    $('join').blur();focused=document.hasFocus();
  }catch(error){await leaveRoom(false);message(error.message);}finally{joining=false;}
});
$('leave').addEventListener('click',()=>leaveRoom());
$('change-game').addEventListener('click',()=>{clearInput();pickerOpen=true;library=null;renderGame();renderActivity();reportPicker();});
$('boot-screen').addEventListener('change',()=>{bootChosen=true;});
$('close-picker').addEventListener('click',()=>{pickerOpen=false;pickerTouched=0;renderGame();renderActivity();reportPicker();});
$('stop-game').addEventListener('click',async()=>{if(!confirm('Stop '+roomState.host.game.title+' for everyone? Its memory card is saved.'))return;try{clearInput();await api('game/stop',{});pickerOpen=false;pickerTouched=0;}catch(e){message(e.message);}await refresh();});
$('admin').addEventListener('submit',async event=>{event.preventDefault();try{await api('admin',{pin:$('admin-pin').value});$('admin-pin').value='';await refresh();}catch(e){message(e.message);}});
$('spectate').addEventListener('click',async()=>{try{clearInput();await api('release',{});await refresh();message('You are watching. Choose an open slot to play.');}catch(e){message(e.message);}});
$('sound').addEventListener('click',async()=>{muted=!muted;rtcAudio.muted=muted;try{
  if(transport==='websocket'){
    if(!muted&&!audioContext){const Audio=window.AudioContext||window.webkitAudioContext;if(!Audio)throw new Error('Audio unavailable');audioContext=new Audio();audioGain=audioContext.createGain();audioGain.gain.value=.6;audioGain.connect(audioContext.destination);}
    if(audioContext){await audioContext.resume();audioGain.gain.value=muted?0:.6;audioAt=0;}
  }else{await video.play();await rtcAudio.play();}$('sound').textContent=muted?'Enable sound':'Mute sound';
}catch{message('Your browser blocked audio. Try Enable sound again.');}});
// Fullscreen moves the touch pad over the picture. iPhone Safari cannot make an element fullscreen,
// so the .immersive class alone fills the window there.
const pad=$('touch-pad'),padHome=document.createComment('touch-pad');pad.before(padHome);
function immersive(on){$('screen').classList.toggle('immersive',on);if(on)$('screen').append(pad);else padHome.after(pad);}
function leaveFullscreen(){if(autoImmersive)sidewaysDeclined=true;autoImmersive=false;if(document.fullscreenElement)document.exitFullscreen?.();immersive(false);}
$('fullscreen').addEventListener('click',()=>{autoImmersive=false;immersive(true);$('screen').requestFullscreen?.().catch(()=>{});});
// A phone turned on its side while a game streams gets the same full-window view, picture and touch pad only.
// Turning it upright again, opening the games list or ✕ returns to the page; after ✕ it waits for the next turn.
const sideways=matchMedia('(pointer:coarse) and (orientation:landscape) and (max-height:540px)');
let autoImmersive=false,sidewaysDeclined=false;
function fitSideways(){
  const screen=$('screen'),want=sideways.matches&&!sidewaysDeclined&&screen.classList.contains('playing')&&Boolean(roomState?.host?.game)&&$('game-picker').hidden;
  if(want&&!screen.classList.contains('immersive')){autoImmersive=true;immersive(true);}
  else if(!want&&autoImmersive){autoImmersive=false;if(!document.fullscreenElement)immersive(false);}
}
sideways.addEventListener('change',()=>{sidewaysDeclined=false;fitSideways();});
$('exit-fullscreen').addEventListener('click',leaveFullscreen);
document.addEventListener('fullscreenchange',()=>{if(!document.fullscreenElement)immersive(false);});
video.addEventListener('click',()=>video.play().catch(()=>{}));
window.addEventListener('keydown',event=>{if(!isControlKey(event.code,analog)||editing()||mine<0)return;event.preventDefault();keys.add(event.code);sendInput();});
window.addEventListener('keyup',event=>{if(keys.delete(event.code)){event.preventDefault();sendInput();}});
window.addEventListener('blur',()=>{focused=false;clearInput();});window.addEventListener('focus',()=>{focused=true;});
document.addEventListener('visibilitychange',()=>{if(document.hidden)clearInput();});
window.addEventListener('pagehide',()=>{clearInput();if(token)fetch('api/leave',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:'{}',keepalive:true}).catch(()=>{});});
window.addEventListener('gamepadconnected',updatePads);window.addEventListener('gamepaddisconnected',updatePads);
$('gamepad').addEventListener('change',()=>{clearInput();updatePads();$('gamepad').blur();});
// Pads with sticks: WASD/IJKL move the sticks, and the touch pad shows two thumbsticks.
function setAnalog(on){if(on===analog)return;analog=on;$('touch-pad').classList.toggle('analog',on);$('dpad-keys').textContent=on?'TFGH':'WASD / Arrows';document.querySelectorAll('.analog-only').forEach(e=>e.hidden=!on);clearInput();updatePads();}
function moveStick(n,x,y){sticks[n]=[x,y];const knob=document.querySelector(`[data-stick="${n}"] span`);if(knob)knob.style.transform=`translate(${x*50}%,${y*50}%)`;}
document.querySelectorAll('[data-stick]').forEach(stick=>{
  const n=Number(stick.dataset.stick);
  const follow=event=>{const box=stick.getBoundingClientRect(),r=box.width/2;let x=(event.clientX-box.left-r)/r,y=(event.clientY-box.top-r)/r;const d=Math.hypot(x,y);if(d>1){x/=d;y/=d;}moveStick(n,x,y);sendInput();};
  stick.addEventListener('pointerdown',event=>{event.preventDefault();if(mine<0)return;stick.setPointerCapture(event.pointerId);stick.classList.add('pressed');follow(event);});
  stick.addEventListener('pointermove',event=>{if(stick.hasPointerCapture(event.pointerId))follow(event);});
  const release=()=>{stick.classList.remove('pressed');moveStick(n,0,0);sendInput();};
  stick.addEventListener('pointerup',release);stick.addEventListener('pointercancel',release);stick.addEventListener('lostpointercapture',release);
});
document.querySelectorAll('[data-button]').forEach(button=>{
  button.addEventListener('pointerdown',event=>{event.preventDefault();if(mine<0)return;button.setPointerCapture(event.pointerId);touches.set(event.pointerId,Number(button.dataset.button));button.classList.add('pressed');sendInput();});
  const release=event=>{touches.delete(event.pointerId);if(![...touches.values()].includes(Number(button.dataset.button)))button.classList.remove('pressed');sendInput();};
  button.addEventListener('pointerup',release);button.addEventListener('pointercancel',release);button.addEventListener('lostpointercapture',release);
});
function pollController(){sendInput();pollPicker();requestAnimationFrame(pollController);}requestAnimationFrame(pollController);
setInterval(()=>sendInput(),100);
setInterval(()=>{send({t:'ping',sent:performance.now()});void refresh();},1000);
setInterval(async()=>{
  if(!controlReady())return;
  if(pc&&transport==='webrtc'){const stats=await pc.getStats();stats.forEach(stat=>{if(stat.type==='inbound-rtp'&&stat.kind==='video'){
    frames=stat.framesDecoded||0;
    if(Number.isFinite(stat.jitterBufferDelay)&&Number.isFinite(stat.jitterBufferEmittedCount)){
      const count=stat.jitterBufferEmittedCount-(previousJitter?.count||0),delay=stat.jitterBufferDelay-(previousJitter?.delay||0);
      if(count>0)receiverBufferMs=1000*delay/count;previousJitter={count:stat.jitterBufferEmittedCount,delay:stat.jitterBufferDelay};
    }
  }});}
  const now=performance.now();streamFPS=Math.round((frames-lastFrames)*1000/(now-lastStats));lastFrames=frames;lastStats=now;
  $('stream-info').textContent=(transport==='webrtc'?'WebRTC':'Fallback')+' · '+streamFPS+' fps'+(rtt===null?'':' · '+rtt+' ms RTT');document.querySelector('.live').classList.toggle('active',streamFPS>0);renderDiagnostics();
},2000);
window.streamRoom={get state(){return roomState;},get pc(){return pc;},get frames(){return frames;},get player(){return mine+1;},get transport(){return transport;},get audioPackets(){return audioPackets;},get diagnostics(){return {receiverBufferMs,receivedToDisplayMs,rtt,emulationHz:roomState?.host.emulationHz,inputPollMs:roomState?.inputPollMs};}};
renderSlots();updatePads();
