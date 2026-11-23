function ptycho_recon_app(params_file)
%PTYCHO_RECON_APP  Entry point of the compiled ptycho reconstruction (mcc -m).
%
%   ptycho_recon_app <scan>.json
%
%   Runs one scan with the values the workflow generator wrote into the
%   parameter file, in place of the values param_config wrote into a copy of
%   the template. The template itself runs as ptycho_recon_<mode>.m, which
%   reads them from P (see ptycho_params.py). Started in the job directory,
%   which is the experiment folder the template expects (exp_dir).

P = read_params(params_file);

% Scripts run in this function's workspace, where P is
switch P.mode
    case 'multislice'
        ptycho_recon_multislice
    case 'singleslice'
        ptycho_recon_singleslice
    otherwise
        error('ptycho_recon_app:mode', 'Unknown mode "%s" in %s', P.mode, params_file)
end
end


function P = read_params(params_file)
P = jsondecode(fileread(params_file));
for f = fieldnames(P)'
    v = P.(f{1});
    if isempty(v) && ~ischar(v)
        % null: an empty sheet cell, which param_config wrote as nan
        P.(f{1}) = NaN;
    elseif isnumeric(v)
        % jsondecode returns [128,128] as a column
        P.(f{1}) = reshape(v, 1, []);
    end
end

% Paths only known in the job. fold_slice is compiled into the app; its
% paths point at the job directory, where fold_slice may write its process
% ID file (utils/.tmp_procID)
P.exp_dir = [pwd '/'];
P.cSAXS_matlab_path = pwd;
P.ptycho_matlab_path = pwd;
end
