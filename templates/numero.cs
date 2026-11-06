class Numero
{
    private int n;

    public Numero(int n){
        this.n=n;
    }
    public static Numero operator+(Numero n1, Numero n2){
       return new Numero(n1.n + n2.n);
    }
    public static Numero operator-(Numero n1, Numero n2){
        return new Numero(n1.n - n2.n);
    }
} 