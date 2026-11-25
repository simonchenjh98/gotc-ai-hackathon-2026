'use strict';

// One scheduled read per view, with a deadline and a single pending refresh.
function makeLivePoller({read,apply,status,alive,isHidden,subscribe,schedule=setTimeout,cancel=clearTimeout}){
  let timer=null,controller=null,inFlight=null,stopped=false,pending=false;
  let delay=2000;
  function clear(){if(timer!==null)cancel(timer);timer=null;}
  function arm(wait){clear();if(!stopped && alive() && !isHidden())timer=schedule(check,wait);}
  function check(){
    timer=null;if(stopped || !alive() || isHidden())return;
    if(!inFlight)inFlight=readOnce().finally(()=>{inFlight=null;});
    return inFlight;
  }
  let deadline=null;
  async function readOnce(){
    const request=new AbortController();controller=request;
    let timedOut=false;
    deadline=schedule(()=>{timedOut=true;request.abort();},8000);
    try{
      const data=await read(request.signal);
      if(!stopped && alive() && !isHidden() && !request.signal.aborted){
        apply(data);status('updated');delay=2000;
      }
    }catch(error){
      if(stopped || !alive() || isHidden() || (request.signal.aborted && !timedOut))return;
      if(error.status===401 || error.status===403){stop();status('auth',error);}
      else{status('error',error);delay=Math.min(delay*2,30000);}
    }finally{
      if(deadline!==null)cancel(deadline);
      deadline=null;controller=null;
      if(!stopped){arm(pending?0:delay);pending=false;}
    }
  }
  function kick(){
    if(stopped)return;
    if(isHidden()){clear();controller?.abort();return;}
    if(inFlight){pending=true;return;}
    arm(0);
  }
  function stop(){
    stopped=true;clear();if(deadline!==null)cancel(deadline);deadline=null;
    controller?.abort();unsubscribe?.();
  }
  const unsubscribe=subscribe(kick);
  return {start(){arm(2000);},refresh(){clear();return check();},kick,stop};
}
