(function ($) {
  'use strict';
  var csrf=$('meta[name="csrf-token"]').attr('content')||'';

  $('#migration-export-form').on('submit',function(event){
    var $form=$(this), password=String($form.find('[name=password]').val()||''), repeat=String($form.find('[name=repeat_password]').val()||'');
    if(password!==repeat){event.preventDefault();DPM.toast('Archive passwords do not match','error');return;}
    $form.find('button[type=submit]').prop('disabled',true).text('BUILDING ARCHIVE…');
    setTimeout(function(){$form.find('button[type=submit]').prop('disabled',false).text('DOWNLOAD ZIP ↓');},15000);
  });

  // Choose an archive before native form validation runs.
  $('#migration-import-form button[type=submit]').on('click',function(event){
    var file=this.form.elements.archive;
    if(!file.files.length){
      event.preventDefault();
      file.click();
    }
  });

  $('#migration-import-form').on('submit',async function(event){
    event.preventDefault();
    var form=this, $button=$(form).find('button[type=submit]'), data=new FormData(form);
    $button.prop('disabled',true).text('IMPORTING…');
    $('#migration-result').addClass('hidden').empty();
    try{
      var response=await fetch(DPM.basePath+'/api/migration/import',{method:'POST',headers:{'X-CSRF-Token':csrf},body:data,credentials:'same-origin'});
      var payload=await response.json();
      if(!response.ok||!payload.ok)throw new Error(payload.error||'Import failed');
      var result=payload.result||{}, projects=result.projects||[], volumes=result.volumes||[], warnings=result.warnings||[];
      var html='<strong>IMPORT COMPLETE</strong>'+
        '<span>'+DPM.escape(projects.length)+' project(s), '+DPM.escape(result.files||0)+' config file(s), '+DPM.escape(volumes.length)+' volume(s).</span>';
      if(projects.length)html+='<small>'+projects.map(function(item){return DPM.escape(item.name)+' #'+DPM.escape(item.id);}).join(' · ')+'</small>';
      if(warnings.length)html+='<ul>'+warnings.map(function(item){return '<li>'+DPM.escape(item)+'</li>';}).join('')+'</ul>';
      $('#migration-result').html(html).removeClass('hidden');
      DPM.toast('Migration imported. Projects are stopped.','success');
      form.reset();
    }catch(error){
      DPM.toast(error.message||'Import failed','error');
    }finally{
      $button.prop('disabled',false).text('IMPORT ARCHIVE ↑');
    }
  });
})(window.jQuery);
