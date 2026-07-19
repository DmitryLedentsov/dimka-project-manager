(function ($, DPM) {
  'use strict';
  var refreshTimer=null;
  var activeLogProject=null;
  var activeLogName='';

  function serviceRow(service){
    var status=service.status||'unknown';
    var firstDeployment=!service.deployed_commit&&status!=='running';
    var deploymentWorking=firstDeployment&&(service.deploy_status==='deploying'||service.deploy_status==='queued');
    var initialBuildFailed=firstDeployment&&service.deploy_status==='failed';
    var displayStatus=initialBuildFailed?'build_failed':deploymentWorking?(service.deploy_stage||service.deploy_status):status;
    var error=service.last_error||service.project_error;
    var runtime=service.pid?DPM.formatUptime(service.uptime_seconds)+'<small>'+DPM.escape(service.memory_mb)+' MB</small>':'—';
    var control;
    if(initialBuildFailed){
      control='<button class="mini-action js-project-log" data-project="'+service.project_id+'" data-name="'+DPM.escape(service.project_name)+'">Logs</button>';
    }else if(deploymentWorking){
      control='<button class="mini-action" disabled>Deploying</button>';
    }else{
      var action=status==='running'?'stop':'start';
      var actionLabel=status==='running'?'Stop':'Start';
      control='<button class="mini-action js-service-action" data-id="'+service.id+'" data-action="'+action+'">'+actionLabel+'</button><a class="mini-action icon-only" href="'+DPM.basePath+'/services/'+service.id+'">↗</a>';
    }
    return '<tr class="service-row" data-search="'+DPM.escape((service.project_name+' '+service.name+' '+service.repository_url).toLowerCase())+'"><td><a class="service-identity" href="'+DPM.basePath+'/services/'+service.id+'"><span class="service-glyph"><i></i><i></i></span><span><strong>'+DPM.escape(service.name)+'</strong><small>'+DPM.escape(service.project_name)+'</small></span></a></td><td><span class="status-pill '+DPM.statusClass(displayStatus)+'"><i></i><span>'+DPM.escape(String(displayStatus).replaceAll('_',' ').toUpperCase())+'</span></span>'+(error?'<span class="row-error" title="'+DPM.escape(error)+'">!</span>':'')+'</td><td><div class="repo-cell"><strong>'+DPM.escape(service.branch)+'</strong><small>'+DPM.escape(service.repository_url)+'</small></div></td><td><code class="commit-chip">'+DPM.shortSha(service.deployed_commit)+'</code></td><td><div class="runtime-cell">'+runtime+'</div></td><td><div class="row-actions">'+control+'</div></td></tr>';
  }

  function issueCard(project){
    return '<article class="issue-card"><div class="issue-symbol">!</div><div><strong>'+DPM.escape(project.name)+'</strong><span>'+DPM.escape(project.deploy_stage||project.deploy_status)+'</span><p>'+DPM.escape(project.last_error||'Deployment failed')+'</p></div><div class="issue-actions"><button class="mini-action js-project-log" data-project="'+project.id+'" data-name="'+DPM.escape(project.name)+'">Logs</button><button class="mini-action js-project-deploy" data-id="'+project.id+'">Retry</button></div></article>';
  }

  function render(data){
    $('#metric-services').text(data.stats.services);
    $('#metric-running').text(data.stats.running);
    var failedProjectIds={};
    data.issues.forEach(function(project){failedProjectIds[project.id]=true;});
    var independentServiceFailures=data.services.filter(function(service){return (service.status==='failed'||service.status==='unhealthy')&&!failedProjectIds[service.project_id];}).length;
    $('#metric-failed').text(data.issues.length+independentServiceFailures);
    $('#metric-projects').text(data.projects.length);
    var health=data.stats.services?Math.round((data.stats.running/data.stats.services)*100):100;
    $('#health-percent').text(health+'%');
    var $body=$('#services-body').empty();
    data.services.forEach(function(service){$body.append(serviceRow(service));});
    $('#empty-services').toggleClass('hidden',data.services.length>0);
    $('.service-table').toggleClass('hidden',data.services.length===0);
    var deploying=data.projects.filter(function(project){return project.deploying||project.deploy_status==='deploying';});
    if(deploying.length){
      var active=deploying[0];
      $('#active-alert-title').text('Deploying '+active.name);
      $('#active-alert-copy').text((active.deploy_stage||'working').replaceAll('_',' ')+' · '+DPM.shortSha(active.remote_commit));
      $('#active-alert').removeClass('hidden');
    }else{
      $('#active-alert').addClass('hidden');
    }
    var $issues=$('#issues-list').empty();
    data.issues.forEach(function(project){$issues.append(issueCard(project));});
    $('#issues-panel').toggleClass('hidden',data.issues.length===0);
    applyFilter();
  }

  function loadDashboard(silent){
    if(!silent)$('#refresh-dashboard').addClass('rotating');
    DPM.api('GET','/dashboard').done(render).always(function(){$('#refresh-dashboard').removeClass('rotating');});
  }

  function applyFilter(){
    var query=String($('#global-filter').val()||'').toLowerCase().trim();
    $('.service-row').each(function(){$(this).toggle(!query||String($(this).data('search')).indexOf(query)!==-1);});
  }

  function loadProjectLog(projectId,projectName,open){
    activeLogProject=Number(projectId);
    activeLogName=projectName||('project '+projectId);
    $('#deployment-log-title').text(activeLogName+' deployment');
    $('#deployment-log-meta').text('PROJECT '+activeLogProject+' / LATEST ATTEMPT');
    if(open){
      $('#deployment-log-console').text('Loading deployment log...');
      DPM.openModal('#deployment-log-modal');
    }
    DPM.api('GET','/projects/'+activeLogProject+'/logs?lines=1000').done(function(data){
      $('#deployment-log-console').text(data.logs||'Deployment log is empty.');
      var node=$('#deployment-log-console').get(0);
      if(node)node.scrollTop=node.scrollHeight;
    });
  }

  $(document).on('click','.js-service-action',function(event){
    event.preventDefault();
    var $button=$(this).prop('disabled',true);
    DPM.api('POST','/services/'+$button.data('id')+'/'+$button.data('action'),{}).done(function(){DPM.toast('Service command completed','success');loadDashboard(true);}).always(function(){$button.prop('disabled',false);});
  });

  $(document).on('click','.js-project-deploy',function(){
    var $button=$(this).prop('disabled',true);
    DPM.api('POST','/projects/'+$button.data('id')+'/deploy',{}).done(function(){DPM.toast('Deployment queued','success');loadDashboard(true);}).always(function(){$button.prop('disabled',false);});
  });

  $(document).on('click','.js-project-log',function(){
    loadProjectLog($(this).data('project'),String($(this).data('name')||''),true);
  });

  $('#deployment-log-refresh').on('click',function(){
    if(activeLogProject)loadProjectLog(activeLogProject,activeLogName,false);
  });
  $('#refresh-dashboard').on('click',function(){loadDashboard(false);});
  $('#global-filter').on('input',applyFilter);
  $(document).on('dpm:project-added',function(){setTimeout(function(){loadDashboard(true);},350);});
  loadDashboard(true);
  refreshTimer=setInterval(function(){loadDashboard(true);},4000);
  $(window).on('beforeunload',function(){clearInterval(refreshTimer);});
})(window.jQuery, window.DPM);
