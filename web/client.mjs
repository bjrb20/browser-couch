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
function editing(){return /INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName);}
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
function renderGame(){$('game-picker').hidden=!token||!roomState||Boolean(roomState.host.game);}
async function refresh(){
  if(!token||polling)return;polling=true;
  try{roomState=await api('state');renderSlots();renderGame();const hz=roomState.host.emulationHz;$('host-info').textContent=!roomState.host.game?'No game loaded':roomState.host.core+' · '+(Number.isFinite(hz)?Math.round(hz)+'/':'')+roomState.host.fps+' Hz game'+(roomState.host.multitap==='none'?'':' · '+roomState.host.multitap+' multitap');if(roomState.host.console){$('pad-label').textContent='KEYBOARD → '+roomState.host.pad.toUpperCase();setAnalog(!!roomState.host.analog);$('game-label').textContent='ORIGINAL '+roomState.host.console+' GAME · BROWSER STREAMING';}if(roomState.host.error)message('The emulator stopped: '+roomState.host.error);renderDiagnostics();}
  catch(e){message(e.message);}finally{polling=false;}
}
async function leaveRoom(notify=true){
  clearInput();const old=token;token=null;mine=-1;roomState=null;
  if(old)try{await fetch('api/leave',{method:'POST',headers:{Authorization:'Bearer '+old,'Content-Type':'application/json'},body:'{}'});}catch{}
  const oldPC=pc;pc=null;channel=null;if(oldPC)oldPC.close();
  const oldSocket=socket;socket=null;fallbackReady=false;if(oldSocket)oldSocket.close();
  latestJPEG=null;$('fallback').hidden=true;video.hidden=false;
  if(audioContext){void audioContext.close().catch(()=>{});audioContext=null;audioGain=null;}muted=true;audioAt=0;video.muted=true;$('sound').textContent='Enable sound';
  incoming.getTracks().forEach(t=>{t.stop();incoming.removeTrack(t);});video.srcObject=null;
  incomingAudio.getTracks().forEach(t=>{t.stop();incomingAudio.removeTrack(t);});rtcAudio.srcObject=null;rtcAudio.muted=true;
  lastSentMask=null;lastSentAt=0;receiverBufferMs=null;receivedToDisplayMs=null;previousJitter=null;
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
function pollController(){sendInput();requestAnimationFrame(pollController);}requestAnimationFrame(pollController);
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
