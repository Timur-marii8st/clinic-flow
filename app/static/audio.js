'use strict';
// Fixed local WAV clips. No browser speech synthesis or external audio.
// Queue only newly observed call versions; stop immediately when superseded.
(function(){
  const clips=['intro','room','generic','ready',...'0123456789'];
  class ClinicAudio {
    constructor(onError=()=>{}){
      this.onError=onError;this.ctx=null;this.buffers=new Map();this.enabled=false;
      this.active=new Map();this.pending=[];this.current=null;this.source=null;this.run=0;
    }
    async enable(){
      const Audio=window.AudioContext||window.webkitAudioContext;
      if(!Audio)throw new Error('Локальная озвучка не поддерживается браузером');
      if(!this.ctx)this.ctx=new Audio();
      await this.ctx.resume();
      if(this.buffers.size!==clips.length){
        this.buffers=new Map(await Promise.all(clips.map(async name=>{
          const res=await fetch('/static/audio/'+name+'.wav',{signal:AbortSignal.timeout(5000)});
          if(!res.ok)throw new Error('Локальные WAV не установлены');
          return [name,await this.ctx.decodeAudioData(await res.arrayBuffer())];
        })));
      }
      this.enabled=true;
      this.play('ready',this.run).catch(()=>this.fail());
    }
    fail(){this.disable();this.onError('Не удалось воспроизвести локальное аудио. Используйте табло.');}
    disable(){this.enabled=false;this.reset();}
    stop(){
      this.run++;
      if(this.source){try{this.source.stop();}catch{}}
      this.source=null;this.current=null;
    }
    reset(){this.stop();this.active.clear();this.pending=[];}
    sync(rows,announce){
      const active=new Map();
      for(const r of rows)if(r.state==='called'&&r.call_version&&/^A-[0-9]+$/.test(r.code||'')){
        const key=r.room_id+'|'+r.call_version;
        active.set(key,r);
        if(announce&&this.enabled&&!this.active.has(key))this.pending.push(key);
      }
      this.active=active;
      this.pending=this.pending.filter(k=>active.has(k));
      if(this.current&&!active.has(this.current))this.stop();
      if(this.enabled)this.drain();
    }
    async play(name,run){
      if(run!==this.run||!this.enabled)return;
      const buffer=this.buffers.get(name);
      if(!buffer||!this.ctx||this.ctx.state!=='running')throw new Error('Audio unavailable');
      await new Promise((resolve,reject)=>{
        const s=this.ctx.createBufferSource();s.buffer=buffer;s.connect(this.ctx.destination);
        this.source=s;
        s.onended=()=>{if(this.source===s)this.source=null;resolve();};
        try{s.start();}catch(e){if(this.source===s)this.source=null;reject(e);}
      });
    }
    async drain(){
      if(this.current||this.source||!this.enabled)return;
      let key;
      while(this.pending.length){
        key=this.pending.shift();
        if(this.active.has(key))break;
        key=null;
      }
      if(!key)return;
      this.current=key;
      const run=this.run,r=this.active.get(key);
      const names=['intro',...r.code.slice(2),...(/^[0-9]+$/.test(r.room_id)?['room',...r.room_id]:['generic'])];
      try{
        for(const name of names){
          if(run!==this.run||!this.active.has(key)||!this.enabled)break;
          await this.play(name,run);
        }
      }catch(e){if(run===this.run)this.fail();}
      finally{if(run===this.run){this.current=null;this.drain();}}
    }
  }
  window.ClinicAudio=ClinicAudio;
})();
