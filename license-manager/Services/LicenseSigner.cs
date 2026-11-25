using System.Security.Cryptography;
using System.Text.Json;
namespace MSUO.LicenseManager;
public sealed class LicenseSigner {
 private readonly RSA rsa=RSA.Create();
 public const string KeyId="msuo-ce-2026-01";
 public LicenseSigner(string privateKeyPem)=>rsa.ImportFromPem(privateKeyPem);
 public string Issue(string uid,string email,string edition="CommunityEdition"){
  var payload=JsonSerializer.SerializeToUtf8Bytes(new {v=1,kid=KeyId,issuer="MSUO",edition,sub=uid,email,issuedAt=DateTimeOffset.UtcNow.ToUnixTimeSeconds()});
  var signature=rsa.SignData(payload,HashAlgorithmName.SHA256,RSASignaturePadding.Pss);
  return "MSUO1."+B64(payload)+"."+B64(signature);
 }
 public static bool Verify(string license,string publicKeyPem,string edition="CommunityEdition"){
  var p=license.Split('.');
  if(p.Length!=3 || p[0]!="MSUO1")return false;
  try{
   using var key=RSA.Create();key.ImportFromPem(publicKeyPem);
   var data=Decode(p[1]);
   if(!key.VerifyData(data,Decode(p[2]),HashAlgorithmName.SHA256,RSASignaturePadding.Pss))return false;
   using var doc=JsonDocument.Parse(data);var j=doc.RootElement;
   return j.GetProperty("v").GetInt32()==1 && j.GetProperty("kid").GetString()==KeyId && j.GetProperty("issuer").GetString()=="MSUO" && j.GetProperty("edition").GetString()==edition && !string.IsNullOrWhiteSpace(j.GetProperty("sub").GetString());
  }catch{return false;}
 }
 private static string B64(byte[] b)=>Convert.ToBase64String(b).TrimEnd('=').Replace('+','-').Replace('/','_');
 private static byte[] Decode(string s)=>Convert.FromBase64String(s.Replace('-','+').Replace('_','/')+new string('=',(4-s.Length%4)%4));
}