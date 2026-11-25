using FirebaseAdmin;
using FirebaseAdmin.Auth;
using Google.Apis.Auth.OAuth2;
using Google.Cloud.Firestore;
using MSUO.LicenseManager;
using System.Threading.RateLimiting;
var builder=WebApplication.CreateBuilder(args);
builder.WebHost.UseUrls(builder.Configuration["BIND_URL"]??"http://127.0.0.1:5107");
builder.Services.AddProblemDetails();
builder.Services.AddRateLimiter(options=>{
 options.RejectionStatusCode=429;
 options.AddPolicy("issuance",ctx=>RateLimitPartition.GetFixedWindowLimiter(ctx.Connection.RemoteIpAddress?.ToString()??"unknown",_=>new FixedWindowRateLimiterOptions{PermitLimit=10,Window=TimeSpan.FromMinutes(1),QueueLimit=0,AutoReplenishment=true}));
});
var projectId=builder.Configuration["FIREBASE_PROJECT_ID"];
var apiKey=builder.Configuration["FIREBASE_WEB_API_KEY"];
var keyPath=builder.Configuration["SIGNING_PRIVATE_KEY_PATH"];
if(string.IsNullOrWhiteSpace(projectId)||string.IsNullOrWhiteSpace(apiKey)||string.IsNullOrWhiteSpace(keyPath))
 throw new InvalidOperationException("FIREBASE_PROJECT_ID, FIREBASE_WEB_API_KEY and SIGNING_PRIVATE_KEY_PATH required");
if(!File.Exists(keyPath))throw new FileNotFoundException("Signing key not found",keyPath);
var credential=GoogleCredential.GetApplicationDefault();
FirebaseApp.Create(new AppOptions{Credential=credential,ProjectId=projectId});
builder.Services.AddSingleton(FirestoreDb.Create(projectId));
builder.Services.AddSingleton(new LicenseSigner(File.ReadAllText(keyPath)));
var app=builder.Build();
app.UseExceptionHandler();app.UseDefaultFiles();app.UseStaticFiles();app.UseRateLimiter();
app.MapGet("/health",()=>Results.Ok(new{status="ready"}));
app.MapGet("/api/config",()=>Results.Ok(new{apiKey,authDomain=$"{projectId}.firebaseapp.com",projectId}));
app.MapPost("/api/license",async(HttpRequest req,FirestoreDb firestore,LicenseSigner signer)=>{
 var auth=req.Headers.Authorization.ToString();
 if(!auth.StartsWith("Bearer ",StringComparison.OrdinalIgnoreCase))return Results.Unauthorized();
 FirebaseToken token;
 try{token=await FirebaseAuth.DefaultInstance.VerifyIdTokenAsync(auth[7..],true);}catch{return Results.Unauthorized();}
 var user=await FirebaseAuth.DefaultInstance.GetUserAsync(token.Uid);
 if(!user.EmailVerified||string.IsNullOrWhiteSpace(user.Email))return Results.Json(new{error="Verify email first."},statusCode:403);
 var doc=firestore.Collection("CommunityEdition").Document(token.Uid);
 var license=await firestore.RunTransactionAsync(async tx=>{
  var snapshot=await tx.GetSnapshotAsync(doc);
  if(snapshot.Exists && snapshot.TryGetValue<string>("licenseKey",out var existing)&&!string.IsNullOrWhiteSpace(existing))return existing;
  var issued=signer.Issue(token.Uid,user.Email!);
  tx.Set(doc,new Dictionary<string,object>{{"uid",token.Uid},{"email",user.Email!},{"edition","CommunityEdition"},{"licenseKey",issued},{"issuedAt",Timestamp.GetCurrentTimestamp()},{"status","active"},{"schemaVersion",1}});
  return issued;
 });
 return Results.Ok(new{licenseKey=license});
}).RequireRateLimiting("issuance");
app.Run();