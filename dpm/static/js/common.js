(function ($) {
  'use strict';
  if (!$) {
    document.body.innerHTML = '<div style="padding:40px;color:white;background:#090b10;font-family:monospace">DPM requires jQuery. Check network access to code.jquery.com.</div>';
    return;
  }
  var basePath = $('meta[name="dpm-base-path"]').attr('content') || '/admin';
  var csrfToken = $('meta[name="csrf-token"]').attr('content') || '';
  window.DPM = {
    basePath: basePath,
    api: function (method, path, payload) {
      return $.ajax({url:basePath+'/api'+path,method:method,contentType:'application/json',dataType:'json',headers:{'X-CSRF-Token':csrfToken},data:payload===undefined?undefined:JSON.stringify(payload)}).catch(function(xhr){var message='Request failed';if(xhr.responseJSON&&xhr.responseJSON.error)message=xhr.responseJSON.error;DPM.toast(message,'error');throw xhr;});
    },
    escape: function (value) {return $('<div>').text(value==null?'':String(value)).html();},
    toast: function (message, type) {var id='toast-'+Date.now()+'-'+Math.floor(Math.random()*1000);var icon=type==='error'?'×':type==='success'?'✓':'◇';var $toast=$('<div>',{id:id,class:'toast '+(type||'info')}).append($('<b>').text(icon)).append($('<span>').text(message));$('#toast-stack').append($toast);requestAnimationFrame(function(){$toast.addClass('visible');});setTimeout(function(){$toast.removeClass('visible');setTimeout(function(){$toast.remove();},300);},3800);},
    formatUptime: function(seconds){if(seconds==null)return '—';seconds=Math.max(0,Number(seconds));var days=Math.floor(seconds/86400),hours=Math.floor((seconds%86400)/3600),minutes=Math.floor((seconds%3600)/60);if(days)return days+'d '+hours+'h';if(hours)return hours+'h '+minutes+'m';return minutes+'m '+Math.floor(seconds%60)+'s';},
    formatDate: function(value){if(!value)return '—';var date=new Date(value);if(isNaN(date.getTime()))return value;return date.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'});},
    statusClass: function(status){status=(status||'unknown').toLowerCase();if(status==='running')return 'status-running';if(status==='failed'||status==='unhealthy')return 'status-failed';if(status==='starting'||status==='stopping'||status==='restarting')return 'status-working';return 'status-stopped';},
    shortSha: function(sha){return sha?String(sha).slice(0,8):'—';}
  };
  $.ajaxSetup({timeout:65000});
  function openModal(selector){$(selector).addClass('open').attr('aria-hidden','false');$('body').addClass('modal-open');setTimeout(function(){$(selector).find('input:visible').first().trigger('focus');},80);}
  function closeModals(){$('.modal-backdrop').removeClass('open').attr('aria-hidden','true');$('body').removeClass('modal-open');}
  $(document).on('click','.js-open-add',function(){openModal('#add-project-modal');});$(document).on('click','.js-open-password',function(){openModal('#password-modal');});$(document).on('click','[data-close-modal]',closeModals);$(document).on('click','.modal-backdrop',function(event){if(event.target===this)closeModals();});$(document).on('keydown',function(event){if(event.key==='Escape')closeModals();if(event.key==='/'&&!$(event.target).is('input,textarea')){event.preventDefault();$('#global-filter').trigger('focus');}});
  $('#mobile-menu').on('click',function(){$('#sidebar').toggleClass('open');$('body').toggleClass('sidebar-open');});
  $('#add-project-form').on('submit',function(event){event.preventDefault();var $form=$(this);var payload={repository_url:$form.find('[name=repository_url]').val(),branch:$form.find('[name=branch]').val(),name:$form.find('[name=name]').val()||null,poll_interval:Number($form.find('[name=poll_interval]').val()||60),auto_update:$form.find('[name=auto_update]').is(':checked')};var $button=$form.find('button[type=submit]').prop('disabled',true).addClass('loading');DPM.api('POST','/projects',payload).done(function(data){DPM.toast('Project queued for deployment','success');closeModals();$form[0].reset();$form.find('[name=branch]').val('master');$form.find('[name=poll_interval]').val('60');$form.find('[name=auto_update]').prop('checked',true);$(document).trigger('dpm:project-added',[data.project]);}).always(function(){$button.prop('disabled',false).removeClass('loading');});});
  $('#password-form').on('submit',function(event){event.preventDefault();var $form=$(this),current=String($form.find('[name=current_password]').val()||''),password=String($form.find('[name=new_password]').val()||''),repeat=String($form.find('[name=repeat_password]').val()||'');if(password!==repeat){DPM.toast('Passwords do not match','error');return;}DPM.api('POST','/account/password',{current_password:current,new_password:password}).done(function(){DPM.toast('Password updated','success');$('#default-credentials-banner').slideUp(180);$form[0].reset();closeModals();});});
  function updateClock(){var now=new Date();$('#live-clock').text(now.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}));}updateClock();setInterval(updateClock,15000);
})(window.jQuery);
