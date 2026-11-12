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
let pickerOpen=false,library=null,libraryAt=0,picking=false,pickerDrawn='',bootChosen=false,consoleFilter='all',coverFor=null;
const coverURLs=new Map(),coverQueue=[];let coverLoads=0;
async function loadLibrary(force=false){if(force||!library||performance.now()-libraryAt>15000){libraryAt=performance.now();try{const fresh=await api('library');if(!bootChosen)$('boot-screen').checked=fresh.bootScreen;library=fresh;}catch(e){message(e.message);}}renderLibrary();}
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
  $('no-match').hidden=!total||shown>0;$('no-match').textContent=query?'No games match “'+$('game-search').value.trim()+'”.':'No games for this console.';
}
function placeholder(art,game,item){art.classList.add('placeholder');const c=document.createElement('b'),t=document.createElement('span');c.textContent=item.id.toUpperCase();t.textContent=game.title;art.replaceChildren(c,t);}
function renderLibrary(){
  const host=roomState?.host,list=$('library'),busy=picking||Boolean(host?.loading),can=Boolean(roomState?.canPick);
  const drawn=JSON.stringify([library,host?.game,host?.loading,host?.error,can,busy,roomState?.adminPin]);if(drawn===pickerDrawn)return;pickerDrawn=drawn;
  $('admin').hidden=can||!roomState?.adminPin;$('stop-game').hidden=!can||!host?.game;$('close-picker').hidden=!host?.game;
  $('picker-eyebrow').textContent=host?.loading?'STARTING':host?.game?'NOW PLAYING · '+host.game.title.toUpperCase():'NO GAME RUNNING';
  $('picker-note').textContent=host?.loading?'Starting '+host.loading+'… Everyone stays in the room.':host?.error&&!host.game?host.error:!library?.consoles.length?'The host has no game library. Start the host with --library to choose games here.':can?(host?.game?'Switching stops this game and saves its memory card first.':'Everyone stays in the room while a game starts.'):roomState?.adminPin?'The host chooses the game. Enter the host PIN to choose one yourself.':'The host has not started a game yet.';
  $('boot-option').hidden=!can||!library?.consoles.some(c=>c.bootScreen);
  const consoles=library?.consoles||[],focusedId=list.contains(document.activeElement)?document.activeElement.closest('.game')?.dataset.id:null;
  if(!consoles.some(c=>c.id===consoleFilter))consoleFilter='all';
  $('picker-tools').hidden=!consoles.some(c=>c.games.length);
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
      if(playing){const tag=document.createElement('em');tag.textContent='PLAYING';b.append(tag);}
      b.addEventListener('click',()=>pick(game,item));card.append(b);
      if(can&&library.covers){
        const tools=document.createElement('div'),change=document.createElement('button');tools.className='cover-tools';
        change.type='button';change.textContent=game.cover?'Change cover':'Add cover';change.setAttribute('aria-label',change.textContent+' for '+game.title);
        change.addEventListener('click',()=>{coverFor=game;$('cover-file').value='';$('cover-file').click();});tools.append(change);
        if(game.cover?.uploaded){const remove=document.createElement('button');remove.type='button';remove.textContent='Remove';remove.setAttribute('aria-label','Remove the cover for '+game.title);
          remove.addEventListener('click',()=>removeCover(game));tools.append(remove);}
        card.append(tools);
      }
      games.append(card);
    }
    box.append(games);list.append(box);
  }
  filterLibrary();
  for(const item of consoles)for(const game of item.games)if(game.cover)queueCover(game);  // now the cards are in the page
  if(focusedId)list.querySelector(`.game[data-id="${focusedId}"] .pick:enabled`)?.focus();
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
  try{await api('game',{id:game.id,bootScreen:item.bootScreen&&$('boot-screen').checked});pickerOpen=false;message('Now playing '+game.title+'.');}catch(e){message(e.message);}finally{picking=false;await refresh();}
}
function renderGame(){
  const game=roomState?.host.game,show=Boolean(token&&roomState&&(!game||pickerOpen));
  $('change-game').hidden=!token||!game||!roomState?.canPick&&!roomState?.adminPin;$('game-picker').hidden=!show;$('screen').classList.toggle('picking',show);
  if(show)void loadLibrary();
}
async function refresh(){
  if(!token||polling)return;polling=true;
  try{roomState=await api('state');renderSlots();renderGame();const hz=roomState.host.emulationHz;$('host-info').textContent=!roomState.host.game?'No game loaded':roomState.host.core+' · '+(Number.isFinite(hz)?Math.round(hz)+'/':'')+roomState.host.fps+' Hz game'+(roomState.host.multitap==='none'?'':' · '+roomState.host.multitap+' multitap');if(roomState.host.console){$('pad-label').textContent='KEYBOARD → '+roomState.host.pad.toUpperCase();setAnalog(!!roomState.host.analog);$('game-label').textContent='ORIGINAL '+roomState.host.console+' GAME · BROWSER STREAMING';}if(roomState.host.error&&roomState.host.game)message('The emulator stopped: '+roomState.host.error);renderDiagnostics();}
  catch(e){message(e.message);}finally{polling=false;}
}
async function leaveRoom(notify=true){
  clearInput();const old=token;token=null;mine=-1;roomState=null;forgetCovers();library=null;pickerDrawn='';
  if(old)try{await fetch('api/leave',{method:'POST',headers:{Authorization:'Bearer '+old,'Content-Type':'application/json'},body:'{}'});}catch{}
  const oldPC=pc;pc=null;channel=null;if(oldPC)oldPC.close();
  const oldSocket=socket;socket=null;fallbackReady=false;if(oldSocket)oldSocket.close();
  latestJPEG=null;$('fallback').hidden=true;video.hidden=false;
  if(audioContext){void audioContext.close().catch(()=>{});audioContext=null;audioGain=null;}muted=true;audioAt=0;video.muted=true;$('sound').textContent='Enable sound';
  incoming.getTracks().forEach(t=>{t.stop();incoming.removeTrack(t);});video.srcObject=null;
  incomingAudio.getTracks().forEach(t=>{t.stop();incomingAudio.removeTrack(t);});rtcAudio.srcObject=null;rtcAudio.muted=true;
  lastSentMask=null;lastSentAt=0;receiverBufferMs=null;receivedToDisplayMs=null;previousJitter=null;
  pickerOpen=false;library=null;bootChosen=false;$('change-game').hidden=true;
  $('join-panel').hidden=false;$('game-picker').hidden=true;$('loading').hidden=true;$('join').disabled=false;$('sound').disabled=true;$('leave').disabled=true;
  $('screen').classList.remove('playing');
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
        try{const data=JSON.parse(event.data);if(data.t==='ready'){sampleRate=data.sampleRate;fallbackReady=true;clearTimeout(timer);resolve();}else if(data.t==='pong')rtt=Math.round(performance.now()-data.sent);}catch{}
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
    channel.addEventListener('message',event=>{try{const data=JSON.parse(event.data);if(data.t==='pong')rtt=Math.round(performance.now()-data.sent);}catch{}});
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
    $('screen').classList.add('playing');
    message(mine===0?'You are Player 1. Use Enter / Start and Cross to navigate the original game.':mine>0?'You are Player '+(mine+1)+'. Wait for Player 1 to choose multiplayer.':'All player slots are occupied. You can watch the game.');
    $('join').blur();focused=document.hasFocus();
  }catch(error){await leaveRoom(false);message(error.message);}finally{joining=false;}
});
$('leave').addEventListener('click',()=>leaveRoom());
$('change-game').addEventListener('click',()=>{clearInput();pickerOpen=true;library=null;renderGame();});
$('boot-screen').addEventListener('change',()=>{bootChosen=true;});
$('close-picker').addEventListener('click',()=>{pickerOpen=false;renderGame();});
$('stop-game').addEventListener('click',async()=>{if(!confirm('Stop '+roomState.host.game.title+' for everyone? Its memory card is saved.'))return;try{clearInput();await api('game/stop',{});pickerOpen=false;}catch(e){message(e.message);}await refresh();});
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
function leaveFullscreen(){if(document.fullscreenElement)document.exitFullscreen?.();immersive(false);}
$('fullscreen').addEventListener('click',()=>{immersive(true);$('screen').requestFullscreen?.().catch(()=>{});});
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
