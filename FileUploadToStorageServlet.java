package com.tgrape.shotly.servlet;

import java.io.File;
import java.io.IOException;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Enumeration;
import java.util.Iterator;
import java.util.List;

import javax.servlet.ServletConfig;
import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

import org.apache.commons.fileupload.FileItem;
import org.apache.commons.fileupload.FileUploadException;
import org.apache.commons.fileupload.disk.DiskFileItemFactory;
import org.apache.commons.fileupload.servlet.ServletFileUpload;
import org.apache.commons.io.FileSystemUtils;
import org.apache.log4j.Logger;

import com.zuntos.common.util.AesUtil;

public class FileUploadToStorageServlet extends HttpServlet {
	
	private static final long serialVersionUID = 1L;
	
	private static final Logger logger = Logger.getLogger(FileUploadToStorageServlet.class);
	
	private ServletConfig config = null;
	
	@Override
	public void init(ServletConfig config) throws ServletException {
		super.init(config);
		this.config = config;
		
		// debug config
		Enumeration en = config.getInitParameterNames();
		while(en.hasMoreElements()){
			String name = (String)en.nextElement();
			String value = config.getInitParameter(name);
			logger.info(name + ":" + value);
		}
	}

	@Override
	protected void service(HttpServletRequest req, HttpServletResponse res) throws ServletException, IOException {
		
		String errCode = "";
		String errMsg = "";
		
		
		
		
		// 2013.04.08 - ServletConfig로 수정
		//int thresHoldSize = 10 * 1024 * 1042; // 10MB 보다 작으면 메모리 저장, 크면 임시폴더에 저장
		//long maxSize = 5 * 1024 * 1024; // 5MB
		//String realDir = "/upload/"; 
		//String tempDir = config.getServletContext().getRealPath("/temp/");
		String tempDir = "/usr/local/temp/";
		System.out.print("tempDir : ");
		System.out.println(tempDir);
		int thresHoldSize = Integer.parseInt(this.config.getInitParameter("threshold_mb")) * 1024 * 1042; // 10MB 보다 작으면 메모리 저장, 크면 임시폴더에 저장
		long maxSize = Long.parseLong(this.config.getInitParameter("max_size_mb")) * 1024 * 1024;
//		String realDir = this.config.getInitParameter("upload_path");
		String allowExt = this.config.getInitParameter("allow_file_extension");
		/*
		File _dir = new File(realDir);
		if( !_dir.exists() ){
			_dir.mkdirs();
			_dir.setReadable(true);
			_dir.setWritable(true);
			_dir.setExecutable(true);
		}
		*/
		
		
		// check request method
		boolean isMultiPart = ServletFileUpload.isMultipartContent(req);
		if( !isMultiPart ) {
			errCode = "100";
			errMsg = "not mulitpart request";
			this.sendResult(errCode, errMsg, res);
			return ;
		}
		
		
		DiskFileItemFactory factory = new DiskFileItemFactory();
		factory.setSizeThreshold( thresHoldSize );
		factory.setRepository(new File(tempDir));
		ServletFileUpload upload = new ServletFileUpload(factory);
		upload.setSizeMax( maxSize );
		upload.setHeaderEncoding("UTF-8");
		
		
		
		try {
			List<FileItem> items = upload.parseRequest(req);
			
			String version = null;
			String fileName = null;
			Iterator<FileItem> iter = items.iterator();
			while( iter.hasNext() ) {
				FileItem item = iter.next();
				if( !item.isFormField() ) {
					logger.debug(item);
					
					/*
					// 2013.04.08 - 서버의 파일시스템 저장공간 체크(관리자 통보)
					long freeSpace_kb = FileSystemUtils.freeSpaceKb(realDir);
					long file_kb = item.getSize() / 1024;
					if( file_kb >= freeSpace_kb ){
						// TODO : this.notifyInsuffSpace();
						errCode = "100";
						errMsg = "Not enough space:" + freeSpace_kb + " kb";
						this.sendResult(errCode, errMsg, res);
						return ;
					}
					*/
					
					// 2013.02.28 - 파일확장자 필터 추가
					fileName = item.getName();
					System.out.print("fileName : ");
					System.out.println(fileName);
					
					String fileExt = fileName.substring(fileName.lastIndexOf(".")+1).toLowerCase();
					if( allowExt.indexOf(fileExt) < 0 ){
						errCode = "110";
						errMsg = "Not allow file extension(" + fileExt + ")";
						this.sendResult(errCode, errMsg, res);
						return ;
					}
					
					
					SimpleDateFormat sf = new SimpleDateFormat("yyyyMMddHHmmssSSS");
					fileName = sf.format(new Date()) + "_"+ fileName ;
					
					//TODO Upload To Storage (2013-11-04, 유근명)
					String FullFileName = tempDir+""+fileName;
					System.out.print("FullFileName : ");
					System.out.println(FullFileName);
					
					
//					String path = config.getServletContext().getRealPath("/upload/");
					String path = "/usr/local/swifttool/";
					
					System.out.print("path : ");
					System.out.println(path);
					
					
					System.out.print("bash : ");
					String cmd = "cd "+path+" & ";
					cmd += "python st -A https://ssproxy.ucloudbiz.olleh.com/auth/v1.0 -K MTM4MTg5MjYwOTEzODE4OTI1MDQzMzcx -U cloud02@tgrape.com upload upload "+FullFileName;
					System.out.println(cmd);
					
					
					Process p = null;
					
					try {
						p = Runtime.getRuntime().exec(new String[]{"bash","-c",cmd});
					} catch(Exception e) {
						System.out.println("Fail Upload To Storage");
						System.out.println(e.getMessage());
					} finally {
						p.destroy();
					}
					

					

					/*
					File uploadFile = new File(realDir, fileName);
					item.write(uploadFile);
					*/
					
					
//					item.delete(); // 임시파일 삭제
					
					
					// 2013.04.08 - 서버 도메인 수정
					errCode = "000";
					//errMsg = "http://14.63.212.150/upload/" + fileName;

				
				} else {
					if("version".equalsIgnoreCase(item.getFieldName())){
						version = item.getString();
					}
				}
				
			}
			logger.info(version);
			if ("v2".equals(version)) {
				errMsg = AesUtil.encrypt(fileName);
			} else {
				errMsg = "http://" + req.getServerName() + "/upload/" + fileName;
			}
			this.sendResult(errCode, errMsg, res);
		} catch (FileUploadException e) {
			logger.error(e.getMessage(), e);
				
			errCode = "990";
			errMsg = e.getMessage();
			this.sendResult(errCode, errMsg, res);
		} catch (Exception e) {
			logger.error(e.getMessage(), e);
			
			errCode = "999";
			errMsg = e.getMessage();
			this.sendResult(errCode, errMsg, res);
		}
	}
	


	private String getParameter(String version) {
		// TODO Auto-generated method stub
		return null;
	}

	private void sendResult(String errCode, String errMsg, HttpServletResponse res) throws IOException{
		StringBuffer buf = new StringBuffer();
		buf.append("<?xml version=\"1.0\" encoding=\"utf-8\"?>");
		buf.append("<result>");
		buf.append("<code>").append(errCode).append("</code>");
		buf.append("<msg>").append(errMsg).append("</msg>");
		buf.append("</result>");
		res.setContentType("text/xml");
		res.setCharacterEncoding("utf-8");
		res.getWriter().write(buf.toString());
		res.getWriter().flush();
		logger.debug(buf.toString());
		
		
	}
}
