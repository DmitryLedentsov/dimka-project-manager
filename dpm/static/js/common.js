(function ($) {
  'use strict';
  if (!$) { document.body.innerHTML='<pre>jQuery failed to load.</pre>'; return; }
  var basePath=$('meta[name="dpm-base-path"]').attr('content')||'/admin';
  var csrfToken=$('meta[name="csrf-token"]').attr('content')||'';
  window.DPM={
    basePath:basePath,
    api:function(method,path,payload){return $.ajax({url:basePath+'/api'+path,method:method,contentType:'application/json',dataType:'json',headers:{'X-CSRF-Token':csrfToken},data:payload===undefined?undefined:JSON.stringify(payload)}).catch(function(xhr){var message=xhr.responseJSON&&xhr.responseJSON.error?xhr.responseJSON.error:'Request failed';DPM.toast(message,'error');throw xhr;});},
    escape:function(value){return $('<div>').text(value==null?'':String(value)).html();},
    shortSha:function(value){return value?String(value).slice(0,9):'—';},
    formatDate:function(value){if(!value)return '—';var date=new Date(value);return isNaN(date.getTime())?value:date.toLocaleString();},
    stateClass:function(value){value=String(value||'unknown').toLowerCase();if(['running','healthy','completed'].includes(value))return 'ok';if(['deploying','building','applying','starting','created'].includes(value))return 'work';if(['failed','unhealthy','invalid','degraded'].includes(value))return 'bad';return 'off';},
    toast:function(message,type){var $item=$('<div class="toast">').addClass(type||'info').text(message);$('#toast-stack').append($item);setTimeout(function(){$item.addClass('show');},10);setTimeout(function(){$item.removeClass('show');setTimeout(function(){$item.remove();},250);},3500);},
    openModal:function(selector){$(selector).addClass('open').attr('aria-hidden','false');$('body').addClass('modal-open');},
    closeModals:function(){$('.modal').removeClass('open').attr('aria-hidden','true');$('body').removeClass('modal-open');}
  };
  $(document).on('click','.js-open-add',function(){DPM.openModal('#add-project-modal');});
  $(document).on('click','.js-open-password',function(){DPM.openModal('#password-modal');});
  $(document).on('click','[data-close-modal]',DPM.closeModals);
  $(document).on('click','.modal',function(event){if(event.target===this)DPM.closeModals();});
  $(document).on('keydown',function(event){if(event.key==='Escape')DPM.closeModals();if(event.key==='/'&&!$(event.target).is('input,textarea')){event.preventDefault();$('#global-filter').trigger('focus');}});
  $('#mobile-menu').on('click',function(){$('#sidebar').toggleClass('open');});
  $('#add-project-form').on('submit',function(event){event.preventDefault();var $form=$(this),$button=$form.find('button[type=submit]').prop('disabled',true);var payload={repository_url:$form.find('[name=repository_url]').val(),branch:$form.find('[name=branch]').val(),name:$form.find('[name=name]').val()||null,compose_file:$form.find('[name=compose_file]').val(),env_file:$form.find('[name=env_file]').val()||null,compose_project_name:$form.find('[name=compose_project_name]').val()||null,poll_interval:Number($form.find('[name=poll_interval]').val()||60),auto_update:$form.find('[name=auto_update]').is(':checked')};DPM.api('POST','/projects',payload).done(function(){DPM.toast('Project registered and queued','success');DPM.closeModals();$form[0].reset();$form.find('[name=branch]').val('master');$form.find('[name=compose_file]').val('compose.yml');$form.find('[name=poll_interval]').val(60);$form.find('[name=auto_update]').prop('checked',true);$(document).trigger('dpm:project-added');}).always(function(){$button.prop('disabled',false);});});
  $('#password-form').on('submit',function(event){event.preventDefault();var $form=$(this),password=String($form.find('[name=new_password]').val()||''),repeat=String($form.find('[name=repeat_password]').val()||'');if(password!==repeat){DPM.toast('Passwords do not match','error');return;}DPM.api('POST','/account/password',{current_password:$form.find('[name=current_password]').val(),new_password:password}).done(function(){DPM.toast('Password updated','success');$('#default-credentials-banner').remove();DPM.closeModals();$form[0].reset();});});
  function clock(){$('#live-clock').text(new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}));}clock();setInterval(clock,15000);
})(window.jQuery);
